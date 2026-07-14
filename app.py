import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Literal, Optional

import csv
import difflib
import glob
import io
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv()  # reads .env in the same folder, if present

app = FastAPI(title="OSINT Signal Console")

APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
SCRAPEBADGER_API_KEY = os.environ.get("SCRAPEBADGER_API_KEY", "")  # scrapebadger.com key — required for X
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Apify actor IDs for the extra Company Intelligence sources. Actor schemas
# change over time — if these stop returning data, check the actor's own
# "Input" tab in Apify Console and update the payload builders below.
INSTAGRAM_ACTOR_ID = os.environ.get("INSTAGRAM_ACTOR_ID", "apify~instagram-scraper")
FACEBOOK_ACTOR_ID = os.environ.get("FACEBOOK_ACTOR_ID", "apify~facebook-posts-scraper")

APIFY_BASE = "https://api.apify.com/v2"
SCRAPEBADGER_BASE = "https://scrapebadger.com/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "search_history.json")
COMPANY_CACHE_FILE = os.path.join(BASE_DIR, "company_cache.json")
INTEL_CACHE_FILE = os.path.join(BASE_DIR, "intelligence_cache.json")
SOURCES_DIR = os.path.join(BASE_DIR, "sources")  # config-driven pluggable data sources
MAX_HISTORY = 100


# ----------------------------- search history ------------------------------

def load_history() -> list[dict]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(entries: list[dict]) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(entries[:MAX_HISTORY], f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # non-fatal — history is a convenience, not critical


def log_history(kind: str, label: str, extra: dict | None = None) -> None:
    """Record a search/profile view, de-duplicating by (kind,label) and moving newest to top."""
    entries = load_history()
    entries = [e for e in entries if not (e.get("kind") == kind and e.get("label") == label)]
    entries.insert(0, {
        "kind": kind,
        "label": label,
        "at": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    })
    save_history(entries)


# ----------------------------- company cache -------------------------------

def load_company_cache() -> dict:
    try:
        with open(COMPANY_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_company_cache(data: dict) -> None:
    try:
        with open(COMPANY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def is_company_url(text: str) -> bool:
    """True if the input looks like a LinkedIn company URL rather than a plain name."""
    t = (text or "").strip().lower()
    return "linkedin.com/company/" in t or t.startswith("http")


def company_slug(company_input: str) -> str:
    """Stable cache key. For a LinkedIn URL, use its /company/<slug> segment."""
    raw = (company_input or "").strip()
    if "/company/" in raw:
        seg = raw.rstrip("/").split("/company/")[-1].split("/")[0].split("?")[0]
        if seg:
            return seg.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or raw.lower()


def cache_company(company_url: str, provider: str, profiles: list[dict]) -> None:
    if not profiles:
        return
    cache = load_company_cache()
    slug = company_slug(company_url)
    name, logo, resolved_url = "", "", ""
    for p in profiles:
        name = name or p.get("company_name") or ""
        logo = logo or p.get("company_logo") or ""
        resolved_url = resolved_url or p.get("company_linkedin_url") or ""
        if name and logo and resolved_url:
            break
    existing = cache.get(slug, {})
    typed_name = company_url.strip() if not is_company_url(company_url) else ""
    # If the user typed a real LinkedIn URL, that's already ground truth for
    # "resolved_url"; otherwise prefer whatever we pulled from a profile.
    best_resolved_url = company_url.strip() if is_company_url(company_url) else (
        resolved_url or existing.get("resolved_url", "")
    )
    cache[slug] = {
        "slug": slug,
        "company_url": company_url,
        "resolved_url": best_resolved_url,
        "name": name or typed_name or existing.get("name") or slug.replace("-", " ").title(),
        "logo": logo or existing.get("logo", ""),
        "provider": provider,
        "count": len(profiles),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles,
    }
    save_company_cache(cache)


def _update_cached_company_url(company_name: str, execs: list[dict]) -> None:
    """Non-destructive companion to cache_company: fills in resolved_url/logo
    from Company Intelligence exec discovery WITHOUT ever touching an existing
    cache entry's 'profiles'/'count' — those may already hold a fuller people
    list from a real LinkedIn tab search, and this must never overwrite that
    with just the small exec subset. Only creates a new (people-less) cache
    entry if this company has never been cached at all."""
    slug = company_slug(company_name)
    resolved_url, logo = "", ""
    for e in execs:
        resolved_url = resolved_url or e.get("company_linkedin_url") or ""
        logo = logo or e.get("company_logo") or ""
        if resolved_url and logo:
            break
    if not resolved_url and not logo:
        return
    try:
        cache = load_company_cache()
        existing = cache.get(slug)
        if existing:
            if resolved_url and is_company_url(resolved_url) and not existing.get("resolved_url"):
                existing["resolved_url"] = resolved_url
            if logo and not existing.get("logo"):
                existing["logo"] = logo
            cache[slug] = existing
        else:
            cache[slug] = {
                "slug": slug, "company_url": company_name,
                "resolved_url": resolved_url if is_company_url(resolved_url) else "",
                "name": company_name, "logo": logo, "provider": "apify_plus",
                "count": 0, "last_updated": datetime.now(timezone.utc).isoformat(),
                "profiles": [],
            }
        save_company_cache(cache)
    except Exception:
        pass  # best-effort only — never let cache bookkeeping break a run


def load_intelligence_cache() -> dict:
    try:
        with open(INTEL_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_intelligence_cache(data: dict) -> None:
    try:
        with open(INTEL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def cache_intelligence_result(result: dict) -> str:
    """Cache a completed Company Intelligence run, keyed by company slug.
    Returns the slug used so the caller can link straight to it."""
    slug = company_slug(result["company_name"])
    cache = load_intelligence_cache()
    cache[slug] = {**result, "slug": slug}
    save_intelligence_cache(cache)
    return slug


Provider = Literal["apify", "apify_plus"]



# ----------------------------- request models -----------------------------

class ProfilesRequest(BaseModel):
    provider: Provider
    company_url: str
    max_items: int = 15
    exec_only: bool = False


class PostsRequest(BaseModel):
    provider: Provider
    profile_url: str
    profile_urn: Optional[str] = None
    max_items: int = 10


class XTweetsRequest(BaseModel):
    handle: str
    max_items: int = 20


class AutomateRequest(BaseModel):
    slug: str
    max_people: int = 15
    posts_per_person: int = 5


class IntelligenceRequest(BaseModel):
    company_name: str
    time_filter: Literal["1d", "1w", "1m"] = "1w"
    max_execs: int = 4
    posts_per_source: int = 8


class SourceExtractRequest(BaseModel):
    docs: str
    hints: str = ""
    api_key: str = ""  # optional inline key to bake into the extracted config


class SourceSaveRequest(BaseModel):
    config: dict


class SourceTestRequest(BaseModel):
    config: dict
    target: str = "Microsoft"
    max_items: int = 3


class SourceDebugRequest(BaseModel):
    config: dict
    error: str = ""
    docs: str = ""
    target: str = "Microsoft"
    max_items: int = 3
    zero_items: bool = False  # true when the last Test call succeeded but returned 0 items
    history: list[dict] = []  # prior debug attempts for this same draft: [{error, zero_items, diagnosis, fixed_config}]
    previous_attempts: list[dict] = []  # prior Debug rounds in this session: what was tried, what happened


# ----------------------------- normalizers --------------------------------

def _guess_x_handle(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", "", name or "").strip().lower()
    return cleaned.replace(" ", "") if cleaned else ""


def _guess_company_handle(company_name: str) -> str:
    """Best-effort social handle guess for a company (used for X/Instagram/Facebook lookups)."""
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", "", company_name or "").strip().lower()
    cleaned = re.sub(r"\b(inc|llc|ltd|corp|corporation|co)\b", "", cleaned).strip()
    return cleaned.replace(" ", "") if cleaned else ""


def _extract_company_info(raw: dict) -> tuple[str, str, str]:
    """Returns (name, logo, linkedin_url). The URL is best-effort — not every
    actor response includes the employer's own company-page URL, but when it
    does, capturing it here lets later Company Intelligence runs fetch that
    company's official posts accurately instead of guessing a URL from the name."""
    candidates = [
        raw.get("currentCompany"),
        raw.get("company"),
        raw.get("employer"),
        raw.get("currentPosition"),
    ]
    exp = raw.get("experience") or raw.get("positions") or raw.get("currentPositions")
    if isinstance(exp, list) and exp and isinstance(exp[0], dict):
        candidates.append(exp[0].get("company") if isinstance(exp[0].get("company"), dict) else exp[0])

    for c in candidates:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("companyName") or c.get("title") or ""
        logo = c.get("logo") or c.get("logoUrl") or c.get("image") or c.get("companyLogo") or ""
        if isinstance(logo, dict):
            logo = logo.get("url", "")
        url = (c.get("linkedinUrl") or c.get("companyPageUrl") or c.get("companyUrl")
               or c.get("url") or c.get("link") or "")
        if isinstance(url, dict):
            url = url.get("url", "")
        if name or logo or url:
            return name, logo, url

    flat_name = raw.get("companyName") or ""
    flat_logo = raw.get("companyLogo") or ""
    if isinstance(flat_logo, dict):
        flat_logo = flat_logo.get("url", "")
    flat_url = raw.get("companyLinkedinUrl") or raw.get("companyUrl") or ""
    return flat_name, flat_logo, flat_url


def _extract_position(raw: dict, headline: str) -> str:
    for key in ("currentPosition", "currentPositions", "positions", "experience"):
        val = raw.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            t = val[0].get("title") or val[0].get("position") or val[0].get("role")
            if t:
                return str(t).strip()
        if isinstance(val, dict):
            t = val.get("title") or val.get("position") or val.get("role")
            if t:
                return str(t).strip()
    for key in ("position", "jobTitle", "title", "occupation"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if headline:
        return headline.split("|")[0].split("@")[0].split(" at ")[0].strip()
    return ""


TIER_ORDER = ["C-Suite", "VP", "Director", "Manager", "Individual Contributor"]
TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}


def _seniority_tier(position: str, headline: str) -> str:
    text = f"{position} {headline}".lower()

    def has_word(*words) -> bool:
        return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)

    if has_word("vp", "svp", "evp") or "vice president" in text:
        return "VP"

    c_suite_acronyms = ["ceo", "cfo", "cto", "cmo", "cio", "coo", "cpo", "cro", "ciso", "cdo", "cxo", "chro"]
    c_suite_words = ["chief", "president", "founder", "co-founder", "owner", "managing partner", "managing director"]
    if has_word(*c_suite_acronyms) or any(w in text for w in c_suite_words) or has_word("partner"):
        return "C-Suite"

    if "director" in text or "head of" in text or text.strip().startswith("head "):
        return "Director"
    if has_word("manager", "lead") or "supervisor" in text:
        return "Manager"
    return "Individual Contributor"


def normalize_profile(raw: dict, provider: str) -> dict:
    company_name, company_logo, company_linkedin_url = _extract_company_info(raw)

    name = " ".join(filter(None, [raw.get("firstName"), raw.get("lastName")])).strip() or raw.get("fullName") or "Unknown"
    photo = raw.get("photo") or raw.get("profilePicture") or ""
    if isinstance(photo, dict):
        photo = photo.get("url", "")
    headline = raw.get("headline") or ""
    position = _extract_position(raw, headline)
    return {
        "name": name,
        "headline": headline,
        "position": position,
        "tier": _seniority_tier(position, headline),
        "url": raw.get("linkedinUrl") or "",
        "urn": raw.get("id") or raw.get("urn") or "",
        "location": (raw.get("location") or {}).get("linkedinText", "") if isinstance(raw.get("location"), dict) else (raw.get("location") or ""),
        "photo": photo,
        "email": raw.get("email") or "",
        "x_handle": _guess_x_handle(name),
        "company_name": company_name,
        "company_logo": company_logo,
        "company_linkedin_url": company_linkedin_url,
    }


_COMPANY_SUFFIX_RE = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|the|group|holdings)\b")


def _normalize_company_text(s: str) -> str:
    """Collapse a company name or LinkedIn URL down to bare comparable words —
    strips protocol/domain, punctuation, and generic suffixes like Inc/LLC."""
    s = (s or "").lower()
    s = re.sub(r"^https?://(www\.)?linkedin\.com/company/", "", s)
    s = s.rstrip("/")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _COMPANY_SUFFIX_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _company_names_match(searched_for: str, profile_company: str) -> bool:
    """
    True only if the profile's actual employer plausibly IS the company we
    searched for. The underlying LinkedIn actor sometimes returns people who
    aren't current employees at all (group members, loose name matches,
    someone who just mentions the company) — this is the guard against that,
    so Company Profiles / Company Intelligence don't quietly attribute a
    random person's posts to the wrong company.
    """
    a = _normalize_company_text(searched_for)
    b = _normalize_company_text(profile_company)
    if not a or not b:
        return False  # can't verify -> treated as unverified, not auto-included
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72


def _headline_mentions_company(searched_for: str, text: str) -> bool:
    """
    Fallback verification when the actor gave us no employer field at all:
    does the person's own headline/position name the company, as a whole
    word? Word-boundary matched on the normalized text so "Apple" doesn't
    also match "Pineapple Consulting". Used to rescue people like a real VP
    whose profile just didn't have a structured company field, without
    rescuing unrelated consultants/coaches who happen to share a market.
    """
    a = _normalize_company_text(searched_for)
    h = _normalize_company_text(text)
    if not a or not h:
        return False
    return re.search(r"\b" + re.escape(a) + r"\b", h) is not None


def filter_profiles_by_company(profiles: list[dict], searched_for: str,
                                strict: bool = False) -> tuple[list[dict], int]:
    """
    Splits normalized profiles into (kept, dropped_count).

    - Employer field present and matches what was searched -> kept, verified.
    - Employer field present but a genuinely different company -> dropped.
    - No employer field returned by the actor at all: fall back to checking
      whether the person's own headline/position names the company (rescues
      real employees whose profile just didn't have a structured company
      field). If that also fails to confirm anything:
        * strict=False (regular LinkedIn tab): kept but flagged
          company_match_verified=False, rather than silently dropping data
          the actor just didn't fill in.
        * strict=True (Company Intelligence exec discovery): dropped. An
          unverifiable stranger — e.g. someone with no employer field and a
          headline like "People call me when their work stops working" —
          must not become a named persona whose unrelated posts get
          attributed to the company in an intelligence report.
    """
    kept = []
    dropped = 0
    for p in profiles:
        pc = p.get("company_name", "")
        if pc:
            if _company_names_match(searched_for, pc):
                kept.append({**p, "company_match_verified": True})
            else:
                dropped += 1
            continue

        if _headline_mentions_company(searched_for, p.get("headline", "")) or \
                _headline_mentions_company(searched_for, p.get("position", "")):
            kept.append({**p, "company_match_verified": True})
            continue

        if strict:
            dropped += 1
        else:
            kept.append({**p, "company_match_verified": False})
    return kept, dropped


def _extract_images(raw: dict) -> list[str]:
    imgs = []
    for key in ("images", "media", "attachments"):
        val = raw.get(key)
        if isinstance(val, list):
            for m in val:
                if isinstance(m, dict):
                    u = m.get("url") or m.get("imageUrl") or m.get("src")
                    mtype = (m.get("type") or "").lower()
                    if u and ("image" in mtype or "photo" in mtype or not mtype):
                        imgs.append(u)
                elif isinstance(m, str):
                    imgs.append(m)
    single = raw.get("image") or raw.get("imageUrl") or raw.get("thumbnail")
    if isinstance(single, str) and single:
        imgs.append(single)
    seen, out = set(), []
    for u in imgs:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def normalize_post(raw: dict, provider: str) -> dict:
    posted_at = raw.get("postedAt") or {}
    date_str = posted_at.get("date") if isinstance(posted_at, dict) else posted_at
    engagement = raw.get("engagement") or {}
    return {
        "text": raw.get("content") or raw.get("text") or "",
        "date": date_str or "",
        "url": raw.get("linkedinUrl") or raw.get("url") or "",
        "reactions": engagement.get("likes", raw.get("reactionsCount", 0)),
        "comments": engagement.get("comments", raw.get("commentsCount", 0)),
        "images": _extract_images(raw),
        "is_reshare": raw.get("_isReshare", False),
    }


# ----------------------------- provider calls -----------------------------

def scrapebadger_tweets(handle: str, max_items: int) -> list[dict]:
    if not SCRAPEBADGER_API_KEY:
        raise HTTPException(400, "SCRAPEBADGER_API_KEY not set on the server.")
    handle = handle.lstrip("@").strip()
    headers = {"x-api-key": SCRAPEBADGER_API_KEY}
    r = requests.get(
        f"{SCRAPEBADGER_BASE}/twitter/users/{handle}/latest_tweets",
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    tweets = body.get("data") or body.get("tweets") or []
    if isinstance(tweets, dict):
        tweets = tweets.get("tweets", [])
    return (tweets or [])[:max_items]


def x_tweets(handle: str, max_items: int) -> list[dict]:
    return scrapebadger_tweets(handle, max_items)


def scrapebadger_search(query: str, max_items: int) -> list[dict]:
    if not SCRAPEBADGER_API_KEY:
        raise HTTPException(400, "SCRAPEBADGER_API_KEY not set on the server.")
    headers = {"x-api-key": SCRAPEBADGER_API_KEY}
    r = requests.get(
        f"{SCRAPEBADGER_BASE}/twitter/tweets/advanced_search",
        headers=headers,
        params={"query": query, "queryType": "Latest"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    tweets = body.get("data") or body.get("tweets") or []
    if isinstance(tweets, dict):
        tweets = tweets.get("tweets", [])
    return (tweets or [])[:max_items]


def x_search_by_company_name(company_name: str, max_items: int) -> list[dict]:
    """
    Searches X directly for posts naming the company, instead of relying on a
    guessed handle — catches the company's own posts (if the handle guess was
    wrong) plus posts about it from press/analysts. Uses ScrapeBadger's
    advanced_search endpoint.
    """
    name = (company_name or "").strip()
    if not name:
        return []
    query = f'"{name}" -filter:replies lang:en'
    return scrapebadger_search(query, max_items)


def normalize_tweet(raw: dict) -> dict:
    imgs = []
    media = raw.get("media")
    if media is None and isinstance(raw.get("extendedEntities"), dict):
        media = raw["extendedEntities"].get("media")
    if media is None:
        media = raw.get("photos") or raw.get("media_urls")
    if isinstance(media, list):
        for m in media:
            if isinstance(m, dict):
                u = m.get("media_url_https") or m.get("url") or m.get("mediaUrl") or m.get("preview_image_url")
                if u:
                    imgs.append(u)
            elif isinstance(m, str):
                imgs.append(m)

    return {
        "text": raw.get("text") or raw.get("full_text") or "",
        "date": raw.get("created_at") or raw.get("createdAt") or "",
        "url": raw.get("url") or raw.get("twitterUrl") or (f"https://x.com/i/status/{raw.get('id')}" if raw.get("id") else ""),
        "reactions": raw.get("favorite_count", raw.get("likeCount", 0)),
        "comments": raw.get("reply_count", raw.get("replyCount", 0)),
        "images": imgs,
        "source": "x",
    }


def _apify_error_detail(r: requests.Response) -> str:
    try:
        body = r.json()
        return body.get("error", {}).get("message") or str(body)[:500]
    except Exception:
        return (r.text or "")[:500] or f"HTTP {r.status_code}"


def _build_profiles_payload(company_url: str, max_items: int, exec_only: bool = False) -> dict:
    payload = {
        "companies": [company_url.strip()],
        "maxItems": max_items,
    }

    if exec_only:
        # Strictly target all definitive top tier decision-making executive roles
        payload["jobTitles"] = [
            "CEO", "CFO", "CTO", "COO", "CMO", "CRO", "CIO", "CHRO",
            "President", "Founder", "Co-Founder", "Managing Director", "Managing Partner"
        ]

    print(f"\n[DEBUG] Apify Profiles Payload: {payload}\n")
    return payload


def _build_posts_payload(profile_url: str, max_items: int) -> dict:
    return {
        "targetUrls": [profile_url],
        "maxPosts": max_items,
        "includeReposts": False,
        "includeQuotePosts": False,
    }


def apify_profiles(company_url: str, max_items: int, exec_only: bool = False) -> list[dict]:
    if not APIFY_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN not set on the server.")
    url = f"{APIFY_BASE}/acts/harvestapi~linkedin-company-employees/run-sync-get-dataset-items"
    payload = _build_profiles_payload(company_url, max_items, exec_only)
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=120)
    if r.status_code >= 400:
        raise HTTPException(502, f"Apify request failed: {_apify_error_detail(r)}")
    return r.json()


def apify_posts(profile_url: str, max_items: int) -> list[dict]:
    if not APIFY_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN not set on the server.")
    url = f"{APIFY_BASE}/acts/harvestapi~linkedin-profile-posts/run-sync-get-dataset-items"
    payload = _build_posts_payload(profile_url, max_items)
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=120)
    if r.status_code >= 400:
        raise HTTPException(502, f"Apify request failed: {_apify_error_detail(r)}")
    raw = r.json()

    def is_reshare(post: dict) -> bool:
        return bool(post.get("repostId") or post.get("repostedBy"))

    for p in (raw or []):
        p["_isReshare"] = is_reshare(p)
    return raw or []


def _resolve_company_linkedin_url(company_input: str) -> str:
    """The company-posts actor (harvestapi~linkedin-company-posts) requires an
    actual LinkedIn company-page URL in targetUrls — unlike the employee-search
    actor, it does not accept a plain name. This resolves a bare name to a URL:
      1. If already a LinkedIn URL, use it as-is.
      2. If this company was searched before (LinkedIn tab or a prior Company
         Intelligence run), reuse whatever real URL got captured then.
      3. Otherwise, fall back to a guessed canonical URL from the slugified
         name. This matches the vast majority of companies whose LinkedIn
         slug equals their name, but isn't guaranteed — if the guess is
         wrong, the actor call fails gracefully (caught by the caller's
         try/except) rather than crashing the run."""
    s = (company_input or "").strip()
    if is_company_url(s):
        return s
    slug = company_slug(s)
    try:
        cached = load_company_cache().get(slug)
        if cached:
            for key in ("resolved_url", "company_url"):
                candidate = cached.get(key, "")
                if candidate and is_company_url(candidate):
                    return candidate
    except Exception:
        pass
    return f"https://www.linkedin.com/company/{slug}/"


def apify_company_posts(company_name_or_url: str, max_items: int) -> list[dict]:
    """Posts from the company's own LinkedIn page (Tier 1 official source).
    Accepts either a plain company name or a full LinkedIn URL — resolves a
    plain name to a real URL first, since the underlying actor's targetUrls
    field requires an actual URL, not a search term."""
    if not APIFY_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN not set on the server.")
    resolved_url = _resolve_company_linkedin_url(company_name_or_url)
    url = f"{APIFY_BASE}/acts/harvestapi~linkedin-company-posts/run-sync-get-dataset-items"
    payload = {"targetUrls": [resolved_url], "maxPosts": max_items}
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=120)
    if r.status_code >= 400:
        raise HTTPException(502, f"Apify request failed: {_apify_error_detail(r)}")
    return r.json() or []


def apify_instagram_posts(handle: str, max_items: int) -> list[dict]:
    """
    Official/Tier-1 Instagram posts via Apify's Instagram Scraper.
    Schema verified against apify/instagram-scraper's documented input shape
    (directUrls + resultsType). If your org uses a different Instagram actor,
    set INSTAGRAM_ACTOR_ID and adjust this payload to match its Input tab.
    """
    if not APIFY_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN not set on the server.")
    handle = handle.lstrip("@").strip()
    if not handle:
        return []
    url = f"{APIFY_BASE}/acts/{INSTAGRAM_ACTOR_ID}/run-sync-get-dataset-items"
    payload = {
        "directUrls": [f"https://www.instagram.com/{handle}/"],
        "resultsType": "posts",
        "resultsLimit": max_items,
    }
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=120)
    if r.status_code >= 400:
        raise HTTPException(502, f"Apify (Instagram) request failed: {_apify_error_detail(r)}")
    return r.json() or []


def normalize_instagram_post(raw: dict) -> dict:
    return {
        "text": raw.get("caption") or "",
        "date": raw.get("timestamp") or raw.get("takenAt") or "",
        "url": raw.get("url") or "",
        "reactions": raw.get("likesCount", 0),
        "comments": raw.get("commentsCount", 0),
        "images": [raw.get("displayUrl")] if raw.get("displayUrl") else [],
        "source": "instagram",
    }


def apify_facebook_posts(handle: str, max_items: int) -> list[dict]:
    """
    Official/Tier-1 Facebook page posts via Apify's Facebook Posts Scraper.
    Schema verified against apify/facebook-posts-scraper's documented input
    shape (startUrls). If your org uses a different Facebook actor, set
    FACEBOOK_ACTOR_ID and adjust this payload to match its Input tab.
    """
    if not APIFY_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN not set on the server.")
    handle = handle.lstrip("@").strip()
    if not handle:
        return []
    url = f"{APIFY_BASE}/acts/{FACEBOOK_ACTOR_ID}/run-sync-get-dataset-items"
    payload = {
        "startUrls": [{"url": f"https://www.facebook.com/{handle}"}],
        "resultsLimit": max_items,
    }
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=120)
    if r.status_code >= 400:
        raise HTTPException(502, f"Apify (Facebook) request failed: {_apify_error_detail(r)}")
    return r.json() or []


def normalize_facebook_post(raw: dict) -> dict:
    return {
        "text": raw.get("text") or raw.get("message") or "",
        "date": raw.get("time") or raw.get("date") or "",
        "url": raw.get("url") or raw.get("postUrl") or "",
        "reactions": raw.get("likes", raw.get("reactionsCount", 0)),
        "comments": raw.get("comments", raw.get("commentsCount", 0)),
        "images": [raw.get("image")] if raw.get("image") else [],
        "source": "facebook",
    }


# ----------------------------- company website (Tier 1) --------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header).*?>.*?</\1>", " ", html)
    text = _TAG_RE.sub(" ", html)
    text = re.sub(r"&nbsp;|&amp;|&#\d+;", " ", text)
    return _WS_RE.sub(" ", text).strip()


def _guess_company_domain(company_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", company_name.lower())
    return f"https://www.{slug}.com" if slug else ""


def fetch_company_website(company_name: str, company_url_hint: str = "") -> tuple[str, str]:
    """
    Best-effort scrape of the company's own homepage/news page — Tier 1.
    Returns (extracted_text, url_used). Deliberately dependency-light (no
    bs4/lxml) since this only needs a rough text blob for the LLM, not
    structured extraction.
    """
    candidates = []
    if company_url_hint:
        candidates.append(company_url_hint)
    guessed = _guess_company_domain(company_name)
    if guessed:
        candidates.append(guessed)

    for url in candidates:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (SignalConsole/1.0)"})
            if r.status_code >= 400 or not r.text:
                continue
            text = _strip_html(r.text)
            if len(text) > 40:
                return text[:4000], url
        except requests.RequestException:
            continue
    return "", ""


# ----------------------------- Google Trends (context, not a "post") -------

_TRENDS_TIMEFRAME = {"1d": "now 1-d", "1w": "now 7-d", "1m": "today 1-m"}


def scrapebadger_trends(company_name: str, time_filter: str) -> dict:
    """
    Google Trends via ScrapeBadger's Google Scraper suite — GET
    /v1/google/trends/interest (2 credits/call), same x-api-key auth as the
    rest of ScrapeBadger. Handles Google's SearchGuard challenge and proxy
    rotation server-side, so it's more reliable than pytrends' unofficial
    scraping and doesn't need a separate library.
    Response shape: {"timeline":[{"date":..., "values":[{"query":..., "value":N}]}],
                      "averages":[{"query":..., "value":N}], "related_queries": {...}}
    """
    if not SCRAPEBADGER_API_KEY:
        raise HTTPException(400, "SCRAPEBADGER_API_KEY not set on the server.")
    timeframe = _TRENDS_TIMEFRAME.get(time_filter, "now 7-d")
    headers = {"x-api-key": SCRAPEBADGER_API_KEY}
    r = requests.get(
        f"{SCRAPEBADGER_BASE}/google/trends/interest",
        headers=headers,
        params={"q": company_name, "date": timeframe},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()

    timeline = body.get("timeline") or []
    points, total, count = [], 0.0, 0
    for entry in timeline:
        vals = entry.get("values") or []
        v = vals[0].get("value") if vals else None
        if v is not None:
            points.append({"time": entry.get("date", ""), "value": v})
            total += v
            count += 1

    averages = body.get("averages") or []
    if averages and averages[0].get("value") is not None:
        avg = averages[0]["value"]
    else:
        avg = round(total / count, 1) if count else 0

    return {
        "keyword": company_name,
        "timeframe": timeframe,
        "average_interest": avg,
        "points": points,
        "related_queries": body.get("related_queries", {}),
    }


def pytrends_trends(company_name: str, time_filter: str) -> dict:
    """Fallback Trends source when no ScrapeBadger key is configured — the
    unofficial pytrends client, no API key needed but less reliable at scale."""
    try:
        from pytrends.request import TrendReq
    except ImportError:
        raise RuntimeError("pytrends not installed — pip install pytrends")

    timeframe = _TRENDS_TIMEFRAME.get(time_filter, "now 7-d")
    pytrends = TrendReq(hl="en-US", tz=0)
    pytrends.build_payload([company_name], timeframe=timeframe)
    df = pytrends.interest_over_time()
    if df is None or df.empty:
        return {"keyword": company_name, "timeframe": timeframe, "average_interest": 0, "points": []}

    series = df[company_name] if company_name in df.columns else df.iloc[:, 0]
    points = [{"time": str(idx), "value": int(v)} for idx, v in series.items()]
    avg = round(float(series.mean()), 1) if len(series) else 0
    return {"keyword": company_name, "timeframe": timeframe, "average_interest": avg, "points": points}


def fetch_google_trends(company_name: str, time_filter: str) -> dict:
    """
    External market-interest context for the company. Prefers ScrapeBadger
    (reliable, handles Google's anti-bot layer) when SCRAPEBADGER_API_KEY is
    set; otherwise falls back to pytrends. Returns {} on any failure — Trends
    is supporting context for the LLM, not a hard dependency of the pipeline.
    """
    if SCRAPEBADGER_API_KEY:
        return scrapebadger_trends(company_name, time_filter)
    return pytrends_trends(company_name, time_filter)


def _trends_level(avg_interest: float) -> str:
    if avg_interest >= 66:
        return "High"
    if avg_interest >= 33:
        return "Medium"
    if avg_interest > 0:
        return "Low"
    return "Unknown"


# ----------------------------- Gemini analysis ------------------------------

def call_gemini_analysis(company_name: str, compiled_text: str, trends: dict) -> dict:
    """
    Sends the aggregated OSINT text (+ Google Trends context) to Gemini and
    asks for a structured summary/metrics/sentiment/strategy breakdown.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY not set on the server.")

    trends_blurb = (
        f"Google Trends average interest over the period: {trends.get('average_interest', 'n/a')} "
        f"(0-100 scale, {_trends_level(trends.get('average_interest', 0))} relative interest)."
        if trends else "No Google Trends data was available for this period."
    )

    prompt = f"""You are a competitive intelligence analyst. Analyze the following aggregated public
content about the company "{company_name}" (LinkedIn, X, and their own website, mixed with official
company posts and individual posts from named executives) plus external market interest data from
Google Trends.

{trends_blurb}

--- AGGREGATED CONTENT START ---
{compiled_text[:18000]}
--- AGGREGATED CONTENT END ---

Return ONLY a JSON object with exactly these keys:
- "summary": a high-level 2-4 sentence summary of the company's current focus and messaging.
- "metrics": an array of the 5-9 MOST IMPORTANT specific numbers/metrics/financial figures found
  explicitly mentioned in the content (e.g. "Q2 revenue up 12%"). Prioritize revenue, growth rate,
  funding, headcount, and other hard numbers over vague claims. Fewer than 5 is fine if that's all
  there is — never pad with filler. Empty array if none found. Never return more than 9.
- "sentiment": one short phrase describing overall tone (e.g. "Positive / growth-focused").
- "strategy_insights": a 2-4 sentence read on strategic direction, contrasting internal messaging
  against the external Google Trends interest level where relevant.
"""

    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if r.status_code >= 400:
        raise HTTPException(502, f"Gemini request failed: {r.text[:500]}")

    data = r.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw_text)
    except (KeyError, IndexError, json.JSONDecodeError):
        return {
            "summary": (data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "") or "")[:2000],
            "metrics": [],
            "sentiment": "",
            "strategy_insights": "",
        }

    metrics = parsed.get("metrics", [])
    metrics = metrics[:9] if isinstance(metrics, list) else []
    return {
        "summary": parsed.get("summary", ""),
        "metrics": metrics,
        "sentiment": parsed.get("sentiment", ""),
        "strategy_insights": parsed.get("strategy_insights", ""),
    }


# ----------------------------- automate / CSV report ------------------------

def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    return '"' + s.replace('"', '""') + '"'


def build_automate_csv(company: dict, people: list[dict]) -> str:
    """
    One row per (person, post) so the hierarchy tier and profile details are
    matched onto every post row. People with no posts still get one row.
    """
    cols = [
        "tier", "name", "role", "location", "email", "profile_url",
        "post_date", "post_text", "post_reactions", "post_comments", "post_url",
    ]
    rows = [",".join(cols)]

    by_tier: dict[str, list[dict]] = {}
    for p in people:
        by_tier.setdefault(p.get("tier") or "Individual Contributor", []).append(p)

    for tier in TIER_ORDER:
        group = by_tier.get(tier)
        if not group:
            continue
        for p in group:
            role = p.get("position") or p.get("headline") or ""
            base = [
                _csv_cell(tier),
                _csv_cell(p.get("name") or "Unknown"),
                _csv_cell(role),
                _csv_cell(p.get("location")),
                _csv_cell(p.get("email")),
                _csv_cell(p.get("url")),
            ]
            posts = p.get("posts") or []
            if not posts:
                rows.append(",".join(base + [_csv_cell(""), _csv_cell("(no posts retrieved)"), _csv_cell(0), _csv_cell(0), _csv_cell("")]))
            else:
                for post in posts:
                    rows.append(",".join(base + [
                        _csv_cell(post.get("date")),
                        _csv_cell(post.get("text")),
                        _csv_cell(post.get("reactions", 0)),
                        _csv_cell(post.get("comments", 0)),
                        _csv_cell(post.get("url")),
                    ]))

    return "\ufeff" + "\r\n".join(rows)


def build_batch_csv(people: list[dict]) -> str:
    """
    Same shape as build_automate_csv but includes a company column, since a
    batch import can span many companies in one file. One row per (person, post).
    """
    cols = [
        "company", "tier", "name", "role", "location", "email", "profile_url",
        "post_date", "post_text", "post_reactions", "post_comments", "post_url",
    ]
    rows = [",".join(cols)]

    by_tier: dict[str, list[dict]] = {}
    for p in people:
        by_tier.setdefault(p.get("tier") or "Individual Contributor", []).append(p)

    for tier in TIER_ORDER:
        group = by_tier.get(tier)
        if not group:
            continue
        for p in group:
            role = p.get("position") or p.get("headline") or ""
            base = [
                _csv_cell(p.get("_company", "")),
                _csv_cell(tier),
                _csv_cell(p.get("name") or "Unknown"),
                _csv_cell(role),
                _csv_cell(p.get("location")),
                _csv_cell(p.get("email")),
                _csv_cell(p.get("url")),
            ]
            posts = p.get("posts") or []
            if not posts:
                rows.append(",".join(base + [_csv_cell(""), _csv_cell("(no posts retrieved)"), _csv_cell(0), _csv_cell(0), _csv_cell("")]))
            else:
                for post in posts:
                    rows.append(",".join(base + [
                        _csv_cell(post.get("date")),
                        _csv_cell(post.get("text")),
                        _csv_cell(post.get("reactions", 0)),
                        _csv_cell(post.get("comments", 0)),
                        _csv_cell(post.get("url")),
                    ]))

    return "\ufeff" + "\r\n".join(rows)


# ----------------------------- batch CSV import -----------------------------
#
# Accepts an Apollo-style people export (First Name, Last Name, Title, Company
# Name, Email, Seniority, Person Linkedin Url, Company Linkedin Url, City,
# State, Country, ...) and turns it directly into the same profile shape the
# live Apify search produces — no Apify company-employees call needed, since
# the CSV already *is* the people list. Posts are still pulled live per person.

# Header names we accept, in priority order, matched case/punctuation-insensitively.
_BATCH_FIELD_ALIASES = {
    "first_name": ["First Name", "FirstName"],
    "last_name": ["Last Name", "LastName"],
    "full_name": ["Full Name", "Name"],
    "title": ["Title", "Job Title", "Position"],
    "seniority": ["Seniority"],
    "company_name": ["Company Name", "Company"],
    "company_url": ["Company Linkedin Url", "Company LinkedIn URL", "Company Url"],
    "person_url": ["Person Linkedin Url", "Person LinkedIn Url", "LinkedIn URL", "Profile URL", "Linkedin Url"],
    "email": ["Email"],
    "city": ["City"],
    "state": ["State"],
    "country": ["Country"],
}


def _normalize_header_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _get_batch_field(row: dict, field: str) -> str:
    norm_row = {_normalize_header_key(k): v for k, v in row.items()}
    for candidate in _BATCH_FIELD_ALIASES.get(field, []):
        key = _normalize_header_key(candidate)
        val = norm_row.get(key)
        if val:
            return str(val).strip()
    return ""


def parse_batch_csv(raw_bytes: bytes) -> list[dict]:
    """Parse an uploaded people CSV into profile dicts (same shape as normalize_profile)."""
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    people: list[dict] = []
    for row in reader:
        if not row:
            continue
        first = _get_batch_field(row, "first_name")
        last = _get_batch_field(row, "last_name")
        name = " ".join(filter(None, [first, last])).strip() or _get_batch_field(row, "full_name")
        person_url = _get_batch_field(row, "person_url")
        title = _get_batch_field(row, "title")
        seniority = _get_batch_field(row, "seniority")
        company_name = _get_batch_field(row, "company_name")
        company_url = _get_batch_field(row, "company_url")
        email = _get_batch_field(row, "email")
        location = ", ".join(filter(None, [
            _get_batch_field(row, "city"),
            _get_batch_field(row, "state"),
            _get_batch_field(row, "country"),
        ]))

        if not name and not person_url:
            continue  # unusable row, skip silently

        people.append({
            "name": name or "Unknown",
            "headline": title,
            "position": title,
            "tier": _seniority_tier(title, seniority),
            "url": person_url,
            "urn": "",
            "location": location,
            "photo": "",
            "email": email,
            "x_handle": _guess_x_handle(name),
            "company_name": company_name,
            "company_logo": "",
            "company_url": company_url,
            "source": "csv_import",
        })
    return people


def group_batch_people(people: list[dict]) -> dict:
    """Group parsed CSV rows into companies, keyed by the same slug the live search uses."""
    groups: dict[str, dict] = {}
    for p in people:
        key_source = p.get("company_url") or p.get("company_name") or "unknown-company"
        slug = company_slug(key_source)
        g = groups.setdefault(slug, {
            "slug": slug,
            "name": p.get("company_name") or key_source,
            "company_url": p.get("company_url") or p.get("company_name") or key_source,
            "people": [],
        })
        g["people"].append(p)
    return groups


# ----------------------------- Company Intelligence orchestrator -----------
#
# Single-input pipeline: given a company name, hit every configured source in
# a strict Tier 1 (official) -> Tier 2 (named executives) order, wrapping each
# individual call in its own try/except so one source (or one exec) failing
# never takes down the rest of the run — partial data beats a crashed
# pipeline. Everything is normalized into one flat "item" shape, then handed
# to Gemini for synthesis.

def _make_item(tier: int, persona: str, source: str, author: str, text: str, date: str,
                url: str = "", reactions=0, comments=0, unverified: bool = False, author_photo: str = "") -> dict:
    return {
        "tier": tier,
        "persona": persona,
        "source": source,
        "author": author,
        "author_photo": author_photo or "",
        "text": text or "",
        "date": date or "",
        "url": url or "",
        "reactions": reactions or 0,
        "comments": comments or 0,
        "unverified": unverified,
    }


def _time_filter_cutoff(time_filter: str) -> datetime:
    now = datetime.now(timezone.utc)
    delta = {"1d": timedelta(days=1), "1w": timedelta(weeks=1), "1m": timedelta(days=30)}
    return now - delta.get(time_filter, timedelta(weeks=1))


def _try_parse_date(s: str):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a %b %d %H:%M:%S %z %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _filter_items_by_time(items: list[dict], cutoff: datetime) -> list[dict]:
    """Keep items with a parseable date >= cutoff; keep items with no parseable
    date too (rather than silently dropping content the source didn't timestamp
    cleanly) — they're just not used to prove recency."""
    kept = []
    for it in items:
        dt = _try_parse_date(it.get("date"))
        if dt is None or dt >= cutoff:
            kept.append(it)
    return kept


def run_company_intelligence(company_name: str, time_filter: str, max_execs: int, posts_per_source: int) -> dict:
    started = time.time()
    errors: dict[str, str] = {}
    items: list[dict] = []

    handle_guess = _guess_company_handle(company_name)

    # ---------------- Tier 1: official company sources ----------------

    website_text, website_url = "", ""
    try:
        website_text, website_url = fetch_company_website(company_name)
        if website_text:
            items.append(_make_item(1, "Company Website", "website", company_name, website_text, "", website_url))
    except Exception as e:
        errors["website"] = str(e)

    try:
        raw_company_posts = apify_company_posts(company_name, posts_per_source)
        for p in raw_company_posts:
            norm = normalize_post(p, "apify_plus")
            items.append(_make_item(1, "Official Page (LinkedIn)", "linkedin", company_name,
                                     norm["text"], norm["date"], norm["url"], norm["reactions"], norm["comments"]))
    except Exception as e:
        errors["linkedin_company"] = str(e)

    seen_tweet_ids: set = set()

    try:
        raw_tweets = x_tweets(handle_guess, posts_per_source)
        for t in raw_tweets:
            tid = t.get("id") or t.get("tweet_id")
            if tid:
                seen_tweet_ids.add(tid)
            norm = normalize_tweet(t)
            items.append(_make_item(1, "Official Account (X)", "x", company_name,
                                     norm["text"], norm["date"], norm["url"], norm["reactions"], norm["comments"],
                                     unverified=True))
    except Exception as e:
        errors["x_company"] = str(e)

    try:
        raw_search = x_search_by_company_name(company_name, posts_per_source)
        for t in raw_search:
            tid = t.get("id") or t.get("tweet_id")
            if tid and tid in seen_tweet_ids:
                continue  # already pulled via the handle guess above
            norm = normalize_tweet(t)
            items.append(_make_item(1, "Official Account (X)", "x", company_name,
                                     norm["text"], norm["date"], norm["url"], norm["reactions"], norm["comments"],
                                     unverified=True))
    except Exception as e:
        errors["x_company_search"] = str(e)

    # ---------------- Tier 1 (cont.): config-driven pluggable sources ----------------
    # Every enabled JSON config in sources/ runs here with the same failsafe.
    collect_config_sources(company_name, handle_guess, posts_per_source, items, errors)

    trends: dict = {}
    try:
        trends = fetch_google_trends(company_name, time_filter)
    except Exception as e:
        errors["google_trends"] = str(e)

    # ---------------- Tier 2: named C-level executives ----------------
    # Failsafe: exec discovery or any individual exec's posts failing never
    # blocks the rest of the pipeline — Tier 1 data above is already collected.
    #
    # Two-gate filtering here (this is stricter than the plain LinkedIn tab):
    #   Gate 1 (filter_profiles_by_company, strict=True) — the underlying
    #   actor sometimes returns people who aren't current employees at all
    #   (group members, loose name matches, someone with no employer field
    #   whatsoever). Unverifiable people are dropped rather than kept-but-
    #   flagged, because here they'd become a named persona whose unrelated
    #   posts get attributed to the company in the report.
    #   Gate 2 (exec-tier check) — we explicitly asked the actor for
    #   CEO/CFO/VP/etc. titles, so anyone whose own title isn't C-Suite/VP
    #   tier is a loose match on the title filter, not an executive, even if
    #   their employer does check out.

    execs: list[dict] = []
    try:
        raw_execs = apify_profiles(company_name, max_items=max(max_execs * 2, 10), exec_only=True)
        normalized_execs = [normalize_profile(p, "apify_plus") for p in raw_execs]
        matched_execs, dropped_mismatched = filter_profiles_by_company(
            normalized_execs, company_name, strict=True)

        exec_like = [e for e in matched_execs if e.get("tier") in ("C-Suite", "VP")]
        dropped_non_exec = len(matched_execs) - len(exec_like)

        notes = []
        if dropped_mismatched:
            notes.append(f"{dropped_mismatched} result(s) dropped — unverifiable or reported employer didn't match \"{company_name}\"")
        if dropped_non_exec:
            notes.append(f"{dropped_non_exec} result(s) dropped — matched the company but title wasn't C-Suite/VP")
        if notes:
            errors["exec_filtering"] = "; ".join(notes)

        execs = exec_like[:max_execs]

        # Best-effort: if any matched exec's employer data included the
        # company's own LinkedIn page URL, cache it now so a future Company
        # Intelligence run (or the LinkedIn tab) resolves this company's own
        # posts accurately instead of guessing a URL from the name.
        if matched_execs:
            _update_cached_company_url(company_name, matched_execs)
    except Exception as e:
        errors["exec_discovery"] = str(e)

    company_logo = next((ex.get("company_logo") for ex in execs if ex.get("company_logo")), "")
    if not company_logo:
        # Execs didn't yield a logo (e.g. max_execs=0, exec discovery failed, or
        # none of the matched execs had a logo on their employer field) — fall
        # back to whatever this company's LinkedIn tab search already cached.
        try:
            cached_company = load_company_cache().get(company_slug(company_name))
            if cached_company:
                company_logo = cached_company.get("logo", "")
        except Exception:
            pass
    if company_logo:
        for it in items:
            if it["tier"] == 1 and it["source"] != "website" and not it["author_photo"]:
                it["author_photo"] = company_logo

    for ex in execs:
        role_label = ex.get("position") or ex.get("headline") or "Executive"
        persona = f"{ex.get('name', 'Unknown')} ({role_label})"
        ex_photo = ex.get("photo", "")

        try:
            if ex.get("url"):
                raw_posts = apify_posts(ex["url"], posts_per_source)
                for p in raw_posts:
                    norm = normalize_post(p, "apify_plus")
                    items.append(_make_item(2, persona, "linkedin", ex.get("name", ""),
                                             norm["text"], norm["date"], norm["url"], norm["reactions"], norm["comments"],
                                             author_photo=ex_photo))
        except Exception as e:
            errors[f"linkedin_exec_{ex.get('name', 'unknown')}"] = str(e)
            # graceful fallback: continue to the next source/exec rather than aborting

        try:
            exec_handle = ex.get("x_handle") or _guess_x_handle(ex.get("name", ""))
            if exec_handle:
                raw_tweets = x_tweets(exec_handle, posts_per_source)
                for t in raw_tweets:
                    norm = normalize_tweet(t)
                    items.append(_make_item(2, persona, "x", ex.get("name", ""),
                                             norm["text"], norm["date"], norm["url"], norm["reactions"], norm["comments"],
                                             unverified=True, author_photo=ex_photo))
        except Exception as e:
            errors[f"x_exec_{ex.get('name', 'unknown')}"] = str(e)
            continue

    # ---------------- time filter + AI synthesis ----------------

    cutoff = _time_filter_cutoff(time_filter)
    items = _filter_items_by_time(items, cutoff)

    compiled_lines = []
    if website_text:
        compiled_lines.append(f"[Company Website] {website_text[:1500]}")
    for it in items:
        if it["source"] == "website":
            continue
        compiled_lines.append(f"[{it['persona']} / {it['source']}] {it['date']}: {it['text'][:600]}")
    compiled_text = "\n\n".join(compiled_lines)

    ai_insights = {"summary": "", "metrics": [], "sentiment": "", "strategy_insights": ""}
    try:
        ai_insights = call_gemini_analysis(company_name, compiled_text, trends)
    except Exception as e:
        errors["gemini"] = str(e)

    # ---------------- metrics panel + feed grouping ----------------

    by_persona: dict[str, list[dict]] = {}
    for it in items:
        by_persona.setdefault(it["persona"], []).append(it)
    feed = [
        {"persona": persona, "tier": grp[0]["tier"] if grp else 1, "items": grp}
        for persona, grp in by_persona.items()
    ]
    feed.sort(key=lambda g: (g["tier"], g["persona"]))

    metrics = {
        "posts_analyzed": len(items),
        "sources_covered": sorted({it["source"] for it in items}),
        "execs_covered": len(execs),
        "trending_interest": _trends_level(trends.get("average_interest", 0)) if trends else "Unknown",
        "time_filter": time_filter,
        "errors_count": len(errors),
    }

    return {
        "company_name": company_name,
        "company_logo": company_logo,
        "time_filter": time_filter,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - started, 2),
        "metrics": metrics,
        "ai_insights": ai_insights,
        "trends": trends,
        "feed": feed,
        "items_flat": items,
        "errors": errors,
    }


# ----------------------------- pluggable config-driven sources -------------
#
# Modularity layer. A "source" is a JSON config file in sources/ describing a
# REST/JSON API: where to call, how to authenticate, and how to map the
# response onto our standard post shape (text/date/url/reactions/comments).
# No Python per source — a single generic fetcher reads the config and does
# the work. Add a source by dropping a .json file in sources/ (or paste the
# API's docs into the UI and let Gemini extract the config for you).
#
# Config schema (all keys the fetcher understands):
# {
#   "name": "reddit",                 # unique id (also the filename stem)
#   "display_name": "Reddit",
#   "tier": 1,                        # 1 = official/company, 2 = per-exec (rare for custom)
#   "enabled": true,
#   "target_type": "name",            # "name" -> company name, "handle" -> guessed @handle
#   "method": "GET",
#   "base_url": "https://oauth.reddit.com",
#   "list_endpoint": "/r/{target}/new.json?limit={max_items}",
#   "headers": { "User-Agent": "SignalConsole/1.0" },
#   "query_params": {},
#   "body": null,                     # for POST: JSON body, supports {target}/{max_items}
#   "auth": {
#       "type": "bearer|header|query|none",
#       "key_value": "sk-...",        # inline key (what "paste a doc with your key" means)
#       "key_env": "REDDIT_API_TOKEN",# OR read from env instead of inlining
#       "header_name": "x-api-key",   # for type=header
#       "param_name": "api_key"       # for type=query
#   },
#   "response_path": "data.children[].data",  # dotted path, [] iterates a list
#   "url_prefix": "https://reddit.com",        # optional, prepended to relative post urls
#   "date_is_unix": false,
#   "field_map": {                    # where each standard field lives in a raw item
#       "text": "selftext", "date": "created_utc", "url": "permalink",
#       "reactions": "ups", "comments": "num_comments"
#   },
#   "unverified": true                # mark items as unverified in the feed
# }

_REQUIRED_SOURCE_KEYS = ("name", "base_url", "list_endpoint", "field_map")


def _ensure_sources_dir() -> None:
    try:
        os.makedirs(SOURCES_DIR, exist_ok=True)
    except OSError:
        pass


def _source_config_path(name: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-") or "source"
    return os.path.join(SOURCES_DIR, f"{safe}.json")


def validate_source_config(cfg: dict) -> list[str]:
    """Returns a list of human-readable problems; empty list == valid."""
    problems = []
    if not isinstance(cfg, dict):
        return ["config is not a JSON object"]
    for k in _REQUIRED_SOURCE_KEYS:
        if not cfg.get(k):
            problems.append(f"missing required field: {k}")
    fm = cfg.get("field_map")
    if isinstance(fm, dict):
        if not fm.get("text"):
            problems.append("field_map must at least map 'text'")
    elif fm is not None:
        problems.append("field_map must be an object")
    tier = cfg.get("tier", 1)
    if tier not in (1, 2):
        problems.append("tier must be 1 or 2")
    auth = cfg.get("auth") or {}
    if auth and auth.get("type") not in (None, "none", "bearer", "header", "query"):
        problems.append("auth.type must be one of none/bearer/header/query")
    return problems


def load_source_configs() -> list[dict]:
    """Every source config on disk (enabled or not), sorted by name."""
    _ensure_sources_dir()
    out = []
    for path in sorted(glob.glob(os.path.join(SOURCES_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict) and cfg.get("name"):
                out.append(cfg)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_enabled_source_configs() -> list[dict]:
    return [c for c in load_source_configs() if c.get("enabled", True) and not validate_source_config(c)]


def save_source_config(cfg: dict) -> dict:
    problems = validate_source_config(cfg)
    if problems:
        raise HTTPException(400, "Invalid source config: " + "; ".join(problems))
    _ensure_sources_dir()
    cfg = {**cfg, "enabled": cfg.get("enabled", True)}
    with open(_source_config_path(cfg["name"]), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg


def delete_source_config(name: str) -> bool:
    path = _source_config_path(name)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _redact_source_config(cfg: dict) -> dict:
    """Copy safe for sending to the browser — never leak an inlined API key."""
    c = json.loads(json.dumps(cfg))  # deep copy
    auth = c.get("auth")
    if isinstance(auth, dict) and auth.get("key_value"):
        kv = str(auth["key_value"])
        auth["key_value"] = (kv[:4] + "…" + kv[-2:]) if len(kv) > 8 else "••••"
        auth["_key_set"] = True
    return c


def _get_nested(obj, dotted: str, default=None):
    """Read a value at a dotted path from a single dict (no list iteration)."""
    if not dotted:
        return default
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _walk_response_path(body, path: str) -> list:
    """Resolve response_path into a flat list of raw item dicts. A segment
    ending in [] iterates a list at that point; '' / omitted path means the
    body itself is already the list (or a single item)."""
    if not path:
        return body if isinstance(body, list) else [body]
    current = [body]
    for part in path.split("."):
        iterate = part.endswith("[]")
        key = part[:-2] if iterate else part
        nxt = []
        for c in current:
            val = c.get(key) if isinstance(c, dict) else None
            if key == "" and iterate:  # bare "[]" — current items are the lists
                val = c
            if val is None:
                continue
            if iterate and isinstance(val, list):
                nxt.extend(val)
            else:
                nxt.append(val)
        current = nxt
    return current


def _sub_tokens(s: str, target: str, max_items: int) -> str:
    return str(s).replace("{target}", quote(str(target), safe="")).replace("{max_items}", str(max_items))


def _sub_in_obj(obj, target: str, max_items: int):
    if isinstance(obj, str):
        return _sub_tokens(obj, target, max_items)
    if isinstance(obj, list):
        return [_sub_in_obj(v, target, max_items) for v in obj]
    if isinstance(obj, dict):
        return {k: _sub_in_obj(v, target, max_items) for k, v in obj.items()}
    return obj


def _resolve_source_key(auth: dict) -> str:
    if not isinstance(auth, dict):
        return ""
    if auth.get("key_value"):
        return str(auth["key_value"])
    if auth.get("key_env"):
        return os.environ.get(auth["key_env"], "")
    return ""


def _build_config_request(cfg: dict, target: str, max_items: int) -> dict:
    """Builds the method/url/headers/params/json for a config-driven call
    without executing it — shared by the real fetcher and the raw-debug fetcher
    so they can never drift out of sync with each other."""
    method = (cfg.get("method") or "GET").upper()
    base = (cfg.get("base_url") or "").rstrip("/")
    endpoint = _sub_tokens(cfg.get("list_endpoint") or "", target, max_items)
    url = base + endpoint if endpoint.startswith("/") else (base + "/" + endpoint if endpoint else base)

    headers = dict(cfg.get("headers") or {})
    params = dict(cfg.get("query_params") or {})
    params = {k: _sub_tokens(v, target, max_items) if isinstance(v, str) else v for k, v in params.items()}

    auth = cfg.get("auth") or {}
    atype = (auth.get("type") or "none").lower()
    key = _resolve_source_key(auth)
    if key and atype == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif key and atype == "header":
        headers[auth.get("header_name") or "x-api-key"] = key
    elif key and atype == "query":
        params[auth.get("param_name") or "api_key"] = key

    json_body = cfg.get("body")
    if json_body is not None:
        json_body = _sub_in_obj(json_body, target, max_items)

    return {"method": method, "url": url, "headers": headers, "params": params, "json_body": json_body}


def fetch_config_source(cfg: dict, target: str, max_items: int) -> list[dict]:
    """Generic fetcher: build the request from the config, call it, and return
    the raw list of items located by response_path. Raises on HTTP/network
    error so the orchestrator records it per-source (partial data > crash)."""
    req = _build_config_request(cfg, target, max_items)
    r = requests.request(
        req["method"], req["url"], headers=req["headers"], params=req["params"],
        json=req["json_body"] if (req["method"] != "GET" and req["json_body"] is not None) else None,
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    items = _walk_response_path(body, cfg.get("response_path") or "")
    items = [it for it in items if isinstance(it, dict)]
    return items[:max_items]


def fetch_config_source_raw_body(cfg: dict, target: str, max_items: int) -> dict:
    """Same call as fetch_config_source, but returns the untouched parsed JSON
    body instead of walking response_path. Used by Debug when a call succeeds
    but response_path finds nothing — lets the user (and Gemini) see the real
    shape instead of guessing again blind."""
    req = _build_config_request(cfg, target, max_items)
    r = requests.request(
        req["method"], req["url"], headers=req["headers"], params=req["params"],
        json=req["json_body"] if (req["method"] != "GET" and req["json_body"] is not None) else None,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def normalize_config_item(raw: dict, cfg: dict) -> dict:
    fm = cfg.get("field_map") or {}

    def g(field, default=""):
        path = fm.get(field)
        return _get_nested(raw, path, default) if path else default

    date_val = g("date", "")
    if cfg.get("date_is_unix") and date_val not in ("", None):
        try:
            date_val = datetime.fromtimestamp(float(date_val), tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            pass

    url_val = str(g("url", "") or "")
    prefix = cfg.get("url_prefix") or ""
    if prefix and url_val and url_val.startswith("/"):
        url_val = prefix.rstrip("/") + url_val

    def _num(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return 0

    return {
        "text": str(g("text", "") or ""),
        "date": str(date_val or ""),
        "url": url_val,
        "reactions": _num(g("reactions", 0)),
        "comments": _num(g("comments", 0)),
        "images": [],
    }


def collect_config_sources(company_name: str, handle_guess: str, posts_per_source: int,
                           items: list, errors: dict) -> None:
    """Run every enabled config-driven source and append normalized items.
    Mirrors the failsafe pattern of the built-in sources: one source failing
    is recorded in errors and never aborts the run."""
    for cfg in load_enabled_source_configs():
        name = cfg.get("name", "custom")
        try:
            target = handle_guess if cfg.get("target_type") == "handle" else company_name
            raw_items = fetch_config_source(cfg, target, posts_per_source)
            persona = cfg.get("persona") or cfg.get("display_name") or name
            tier = cfg.get("tier", 1)
            unverified = bool(cfg.get("unverified", True))
            for raw in raw_items:
                norm = normalize_config_item(raw, cfg)
                if not (norm["text"] or norm["url"]):
                    continue
                items.append(_make_item(tier, persona, name, company_name,
                                        norm["text"], norm["date"], norm["url"],
                                        norm["reactions"], norm["comments"],
                                        unverified=unverified))
        except Exception as e:
            errors[f"source_{name}"] = str(e)


# --------- Gemini: extract a source config from pasted API docs -------------

_SOURCE_EXTRACTION_INSTRUCTIONS = """You convert API documentation into a strict JSON config for a data-source plugin.
The plugin calls ONE list endpoint that returns recent posts/items about a company, then maps each item
onto a fixed shape: text, date, url, reactions, comments.

Return ONLY a JSON object (no markdown, no prose) with these keys:
- "name": short lowercase id, e.g. "reddit" or "newsapi"
- "display_name": human label
- "tier": 1 (default) unless the docs clearly describe per-person/executive data (then 2)
- "target_type": "name" if the endpoint takes a company NAME, "handle" if it takes a social handle/username
- "method": "GET" or "POST"
- "base_url": scheme + host, no trailing slash
- "list_endpoint": path beginning with "/", may include query string. Use {target} where the company
  name/handle goes and {max_items} for the result limit.
- "headers": object of static headers (omit auth headers, those go in "auth")
- "query_params": object of static query params (omit auth, use tokens {target}/{max_items} if needed)
- "body": for POST only, the JSON body object (use {target}/{max_items} tokens); null for GET
- "auth": { "type": "none|bearer|header|query", "header_name": "...", "param_name": "..." }
  Do NOT invent a key. Leave key_value/key_env out — the caller injects the key.
- "response_path": dotted path from the JSON response root to the LIST of items. A segment ending in []
  iterates a list. Use "" if the response itself is the list.
- "url_prefix": optional origin to prepend if item urls are relative (e.g. "/r/x" -> full url); else ""
- "date_is_unix": true if the date field is a unix timestamp (seconds), else false
- "field_map": object mapping each of text/date/url/reactions/comments to a dotted path inside ONE item.
  Omit a field if the API doesn't provide it. "text" is required.

If something isn't in the docs, make the most reasonable guess and keep going. Never include commentary."""


def extract_source_config_from_docs(docs_text: str, hints: str = "") -> dict:
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY not set on the server.")
    prompt = (
        _SOURCE_EXTRACTION_INSTRUCTIONS
        + ("\n\nExtra hints from the user:\n" + hints if hints else "")
        + "\n\n--- API DOCUMENTATION START ---\n"
        + (docs_text or "")[:18000]
        + "\n--- API DOCUMENTATION END ---\n"
    )
    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if r.status_code >= 400:
        raise HTTPException(502, f"Gemini request failed: {r.text[:500]}")
    data = r.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        cfg = json.loads(raw_text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise HTTPException(502, f"Could not parse a config out of Gemini's response: {e}")
    cfg.setdefault("enabled", True)
    cfg.setdefault("unverified", True)
    return cfg


# --------- source config debugging (rule-based checks + Gemini second opinion) ---------
#
# Runs after a failed Test. Two layers:
#   1. Fast, free, deterministic checks against common config mistakes (missing
#      auth key, RapidAPI's two-header requirement, obvious path issues) —
#      these catch the majority of real-world failures instantly, no API call.
#   2. If Gemini is configured, hand it the config + error + rule findings and
#      ask for a plain-English diagnosis plus an optional corrected config,
#      for cases the static rules don't recognize.

def _rule_based_source_diagnosis(cfg: dict, error: str, zero_items: bool = False) -> list[str]:
    findings = []
    err_l = (error or "").lower()
    base_url = (cfg.get("base_url") or "").lower()
    headers = {str(k).lower(): v for k, v in (cfg.get("headers") or {}).items()}
    auth = cfg.get("auth") or {}
    atype = (auth.get("type") or "none").lower()

    has_key = bool(auth.get("key_value")) or bool(auth.get("key_env") and os.environ.get(auth["key_env"]))

    if atype in ("bearer", "header", "query") and not has_key:
        findings.append(
            "auth.key_value is empty (and no auth.key_env is set) — the request went out with no "
            "credentials at all. Paste your API key directly into the draft JSON's auth.key_value, "
            "or set auth.key_env to an environment variable name that holds it."
        )

    if "rapidapi.com" in base_url:
        if "x-rapidapi-host" not in headers:
            expected_host = base_url.replace("https://", "").replace("http://", "").rstrip("/")
            findings.append(
                f"This looks like a RapidAPI endpoint. RapidAPI requires an 'X-RapidAPI-Host' header "
                f"(value: '{expected_host}') in addition to the API key — add it to the headers object."
            )
        if atype != "header" or (auth.get("header_name") or "").lower() != "x-rapidapi-key":
            findings.append(
                "RapidAPI expects the key in a header named exactly 'x-rapidapi-key' "
                "(auth.type should be 'header', auth.header_name should be 'x-rapidapi-key')."
            )

    if "401" in err_l or "unauthorized" in err_l:
        findings.append(
            "401 Unauthorized almost always means the auth header/param never reached the server, or "
            "the key itself is wrong — this is usually the auth.key_value issue above, not a code bug."
        )
    if "403" in err_l or "forbidden" in err_l:
        findings.append(
            "403 Forbidden with a key present usually means the key is valid but not subscribed/enabled "
            "for this specific API/endpoint (common on RapidAPI, which requires subscribing per-listing "
            "even on free tiers) — check your account's subscription for this exact API."
        )
    if "404" in err_l or "not found" in err_l:
        findings.append(
            "404 suggests list_endpoint's path is wrong — double check the path against the docs, "
            "including whether {target} should be a raw name vs. a handle (target_type)."
        )
    if "timed out" in err_l or "timeout" in err_l:
        findings.append("Request timed out — base_url may be wrong/unreachable, or the API is slow/down.")
    if "expecting value" in err_l or "jsondecodeerror" in err_l:
        findings.append(
            "The response wasn't valid JSON — the API may have returned an HTML error page instead "
            "(often itself a symptom of bad auth or a wrong URL, not a JSON-shape problem)."
        )

    if zero_items and not err_l:
        findings.append(
            f"The call itself succeeded (auth worked) but response_path '{cfg.get('response_path','')}' "
            "found nothing. Two likely causes: (1) the path is wrong for the real response shape — Gemini "
            "guessed it without ever seeing a live response, or (2) this specific endpoint doesn't return "
            "post/media data at all — many 'profile' endpoints only return account metadata (bio, follower "
            "counts) and posts live under a *different* endpoint (e.g. /user-posts, /user-feed, /media). "
            "Check below for the actual raw response — it'll show which case this is."
        )

    if not findings and not error and not zero_items:
        findings.append("No error provided yet — run Test first, then Debug for a diagnosis of the failure.")

    return findings


_SOURCE_DEBUG_INSTRUCTIONS = """You are debugging a data-source plugin config that failed a live test call
(or succeeded but returned zero items). The config, the error (if any), some automated findings, and
possibly a REAL raw API response are provided below.

If PREVIOUS DEBUG ATTEMPTS are included, this is a repeat: earlier diagnoses/fixes were already tried in
this session and the problem is STILL happening. Do not repeat a previous diagnosis or propose the same
fix again — that already failed. Instead:
1. Look at what changed (or didn't) between attempts and the config now.
2. Explain specifically why the previous fix likely didn't resolve it.
3. Propose something genuinely different: a different endpoint, a different auth mechanism, a param the
   docs didn't mention, evidence the API itself may be broken/rate-limited, etc.

Return ONLY a JSON object with:
- "diagnosis": 2-4 plain-English sentences. If this is a repeat, explicitly address why the prior attempt(s)
  didn't work before proposing the next thing to try.
- "fixed_config": a full corrected config object using the exact same schema as the input config
  (same keys). If a raw response is provided and response_path/field_map look wrong for that real shape,
  fix them precisely against what you see in the raw response — do not guess blind when real data is
  available. Return null only if you truly cannot determine a fix (e.g. the answer requires information
  only the user has, like checking a subscription or re-reading the docs for a different endpoint).
Do not include markdown or commentary outside the JSON."""


def _rule_based_repeat_diagnosis(previous_attempts: list, error: str, zero_items: bool) -> list[str]:
    """Detects when the same symptom is recurring across Debug rounds — the
    static per-symptom rules above already fired once and clearly didn't fix
    it, so repeating them is useless noise. This flags the repeat explicitly."""
    if not previous_attempts:
        return []
    findings = []
    n = len(previous_attempts)
    last = previous_attempts[-1] if isinstance(previous_attempts[-1], dict) else {}
    last_zero = bool(last.get("zero_items"))
    last_err = (last.get("error") or "").strip().lower()
    cur_err = (error or "").strip().lower()

    same_symptom = (zero_items and last_zero) or (bool(cur_err) and cur_err == last_err)
    applied_last_fix = bool(last.get("applied_fix"))

    if same_symptom:
        findings.append(
            f"This is Debug attempt #{n + 1} and the symptom is unchanged from the last attempt"
            + (" (the previously suggested fix was applied but didn't resolve it)" if applied_last_fix else "")
            + " — whatever was diagnosed before isn't the real cause. Worth stepping back: re-check the "
            "actual endpoint URL/path against the provider's docs directly (not just this session's guesses), "
            "confirm the API key is subscribed/enabled for this exact API on the provider's dashboard, and "
            "consider that this endpoint may simply not return the data you need — some 'profile' endpoints "
            "never include posts, no matter how response_path is set."
        )
    elif applied_last_fix:
        findings.append(
            "The previous suggested fix was applied and the symptom changed since then — progress, but not "
            "resolved yet. See the new error/response below for what's different now."
        )
    return findings


def debug_source_config(cfg: dict, error: str, docs: str = "", target: str = "Microsoft",
                        max_items: int = 3, zero_items: bool = False,
                        previous_attempts: list | None = None) -> dict:
    previous_attempts = previous_attempts or []
    rule_findings = _rule_based_source_diagnosis(cfg, error, zero_items)
    rule_findings = _rule_based_repeat_diagnosis(previous_attempts, error, zero_items) + rule_findings
    result = {"rule_findings": rule_findings, "ai_diagnosis": "", "fixed_config": None,
              "raw_response_sample": None, "raw_fetch_error": None}

    # When a call succeeded but returned nothing (or no error was given at all),
    # fetch the real untouched response so both the user and Gemini can see the
    # actual shape instead of reasoning about it blind a second time.
    raw_body = None
    if zero_items or not error:
        try:
            resolved_target = _guess_company_handle(target) if cfg.get("target_type") == "handle" else target
            raw_body = fetch_config_source_raw_body(cfg, resolved_target, max(1, min(max_items, 5)))
            pretty_raw = json.dumps(raw_body, indent=2, ensure_ascii=False)
            result["raw_response_sample"] = pretty_raw[:6000]
        except Exception as e:
            result["raw_fetch_error"] = str(e)

    if not GEMINI_API_KEY:
        return result

    history_block = ""
    if previous_attempts:
        lines = []
        for i, att in enumerate(previous_attempts[-5:], 1):  # cap context size
            lines.append(
                f"Attempt {i}: symptom={'zero items' if att.get('zero_items') else (att.get('error') or 'unknown')} | "
                f"diagnosis given: {att.get('ai_diagnosis') or '(none)'} | "
                f"fix applied by user afterward: {'yes' if att.get('applied_fix') else 'no'}"
            )
        history_block = "\n\n--- PREVIOUS DEBUG ATTEMPTS THIS SESSION (still unresolved) ---\n" + "\n".join(lines)

    prompt = (
        _SOURCE_DEBUG_INSTRUCTIONS
        + "\n\n--- CURRENT CONFIG ---\n" + json.dumps(cfg, indent=2)
        + "\n\n--- ERROR FROM TEST (empty if the call succeeded) ---\n" + (error or "(none — call succeeded)")
        + "\n\n--- AUTOMATED FINDINGS ---\n" + ("\n".join(f"- {f}" for f in rule_findings) or "(none)")
        + (f"\n\n--- REAL RAW RESPONSE FROM THE API (use this to fix response_path/field_map) ---\n{result['raw_response_sample']}" if result["raw_response_sample"] else "")
        + (f"\n\n--- COULD NOT FETCH A SAMPLE RESPONSE ---\n{result['raw_fetch_error']}" if result["raw_fetch_error"] else "")
        + ("\n\n--- ORIGINAL API DOCS (if relevant) ---\n" + docs[:8000] if docs else "")
        + history_block
    )
    try:
        url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        if r.status_code >= 400:
            result["ai_diagnosis"] = f"(Gemini debug call failed: {r.text[:300]})"
            return result
        data = r.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw_text)
        result["ai_diagnosis"] = parsed.get("diagnosis", "")
        fixed = parsed.get("fixed_config")
        if isinstance(fixed, dict) and not validate_source_config(fixed):
            result["fixed_config"] = fixed
    except Exception as e:
        result["ai_diagnosis"] = f"(Gemini debug call failed: {e})"
    return result


def build_intelligence_csv(result: dict) -> str:
    cols = ["tier", "persona", "source", "author", "date", "text", "reactions", "comments", "url", "unverified"]
    rows = [",".join(cols)]
    for it in result.get("items_flat", []):
        rows.append(",".join([
            _csv_cell(it.get("tier")),
            _csv_cell(it.get("persona")),
            _csv_cell(it.get("source")),
            _csv_cell(it.get("author")),
            _csv_cell(it.get("date")),
            _csv_cell(it.get("text")),
            _csv_cell(it.get("reactions", 0)),
            _csv_cell(it.get("comments", 0)),
            _csv_cell(it.get("url")),
            _csv_cell(it.get("unverified", False)),
        ]))
    return "\ufeff" + "\r\n".join(rows)


# ----------------------------- endpoints ----------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "apify_key_present": bool(APIFY_TOKEN),
        "x_key_present": bool(SCRAPEBADGER_API_KEY),
        "x_provider": "scrapebadger" if SCRAPEBADGER_API_KEY else None,
        "gemini_key_present": bool(GEMINI_API_KEY),
    }


@app.post("/api/profiles")
def get_profiles(req: ProfilesRequest):
    started = time.time()

    try:
        raw = apify_profiles(req.company_url, req.max_items, req.exec_only)
    except requests.HTTPError as e:
        raise HTTPException(502, f"{req.provider} request failed: {e}")
    except requests.RequestException as e:
        raise HTTPException(502, f"{req.provider} network error: {e}")

    profiles = [normalize_profile(p, req.provider) for p in (raw or [])]
    # exec_only searches use strict verification — an unverifiable stranger
    # shouldn't show up as a "found exec" any more than in Company Intelligence.
    profiles, dropped_mismatched = filter_profiles_by_company(profiles, req.company_url, strict=req.exec_only)
    for p in profiles:
        p["source"] = req.provider
    log_history("company", req.company_url, {"provider": req.provider, "count": len(profiles)})
    cache_company(req.company_url, req.provider, profiles)
    return {
        "provider": req.provider,
        "company_url": req.company_url,
        "slug": company_slug(req.company_url),
        "count": len(profiles),
        "dropped_mismatched": dropped_mismatched,
        "elapsed_sec": round(time.time() - started, 2),
        "profiles": profiles,
        "errors": {},
    }


@app.post("/api/posts")
def get_posts(req: PostsRequest):
    started = time.time()

    try:
        raw = apify_posts(req.profile_url, req.max_items)
    except requests.HTTPError as e:
        raise HTTPException(502, f"{req.provider} request failed: {e}")
    except requests.RequestException as e:
        raise HTTPException(502, f"{req.provider} network error: {e}")

    posts = [normalize_post(p, req.provider) for p in (raw or [])]
    for p in posts:
        p["source"] = req.provider
    log_history("profile", req.profile_url, {"provider": req.provider, "count": len(posts)})
    return {
        "provider": req.provider,
        "profile_url": req.profile_url,
        "count": len(posts),
        "elapsed_sec": round(time.time() - started, 2),
        "posts": posts,
        "errors": {},
    }


@app.get("/api/history")
def get_history():
    return {"entries": load_history()}


@app.delete("/api/history")
def clear_history():
    save_history([])
    return {"cleared": True}


@app.get("/api/companies")
def list_companies():
    cache = load_company_cache()
    out = [
        {
            "slug": c["slug"],
            "company_url": c["company_url"],
            "name": c["name"],
            "logo": c["logo"],
            "provider": c.get("provider", ""),
            "count": c["count"],
            "last_updated": c["last_updated"],
        }
        for c in cache.values()
    ]
    out.sort(key=lambda c: c["last_updated"], reverse=True)
    return {"companies": out}


@app.get("/api/companies/{slug}")
def get_company(slug: str):
    cache = load_company_cache()
    c = cache.get(slug)
    if not c:
        raise HTTPException(404, "Company not cached yet — search it once from the LinkedIn tab first.")
    return c


@app.post("/api/x")
def get_x_tweets(req: XTweetsRequest):
    started = time.time()
    try:
        raw = x_tweets(req.handle, req.max_items)
    except requests.HTTPError as e:
        raise HTTPException(502, f"x request failed: {e}")
    except requests.RequestException as e:
        raise HTTPException(502, f"x network error: {e}")

    tweets = [normalize_tweet(t) for t in (raw or [])]
    log_history("x", "@" + req.handle.lstrip("@"), {"provider": "x", "count": len(tweets)})
    return {
        "provider": "x",
        "handle": req.handle,
        "count": len(tweets),
        "elapsed_sec": round(time.time() - started, 2),
        "posts": tweets,
        "errors": {},
    }


@app.post("/api/automate")
def automate_company_report(req: AutomateRequest):
    """
    Build the tier hierarchy for a cached company, pull recent posts for each
    person (one Apify call per person, capped by max_people), and hand back a
    single matched CSV: tier -> person -> their posts, one row per post.
    """
    cache = load_company_cache()
    company = cache.get(req.slug)
    if not company:
        raise HTTPException(404, "Company not cached yet — search it once from the LinkedIn tab first.")

    profiles = company.get("profiles") or []
    if not profiles:
        raise HTTPException(400, "This company has no cached people to build a report from.")

    max_people = max(1, min(req.max_people, 50))
    posts_per_person = max(1, min(req.posts_per_person, 15))

    ordered = sorted(profiles, key=lambda p: TIER_RANK.get(p.get("tier"), 99))
    selected = ordered[:max_people]

    enriched = []
    for p in selected:
        posts: list[dict] = []
        if p.get("url"):
            try:
                raw = apify_posts(p["url"], posts_per_person)
                posts = [normalize_post(x, "apify_plus") for x in (raw or [])]
            except Exception:
                posts = []  # keep the person in the report even if their posts fail
            time.sleep(0.15)
        enriched.append({**p, "posts": posts})

    csv_text = build_automate_csv(company, enriched)
    safe_name = re.sub(r"[^a-z0-9]+", "_", (company.get("name") or req.slug).lower()).strip("_") or req.slug
    filename = f"signal_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    return StreamingResponse(
        BytesIO(csv_text.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/batch/preview")
async def batch_preview(file: UploadFile = File(...)):
    """
    Parse an uploaded CSV and show how it grouped into companies/people —
    no LinkedIn calls made, nothing cached. Lets you confirm names matched
    before spending API calls on the real run.
    """
    raw = await file.read()
    try:
        people = parse_batch_csv(raw)
    except Exception as e:
        raise HTTPException(400, f"Could not parse CSV: {e}")
    if not people:
        raise HTTPException(400, "No usable rows found — need at least a name or a LinkedIn profile URL per row.")

    groups = group_batch_people(people)
    companies = []
    for g in groups.values():
        with_url = sum(1 for p in g["people"] if p.get("url"))
        companies.append({
            "slug": g["slug"],
            "name": g["name"],
            "company_url": g["company_url"],
            "count": len(g["people"]),
            "with_linkedin_url": with_url,
            "sample_names": [p["name"] for p in g["people"][:3]],
        })
    companies.sort(key=lambda c: c["count"], reverse=True)

    return {
        "total_rows": len(people),
        "company_count": len(companies),
        "companies": companies,
    }


@app.post("/api/batch/automate")
async def batch_automate(
    file: UploadFile = File(...),
    max_people: int = Form(30),
    posts_per_person: int = Form(3),
):
    """
    Full batch run: parse the CSV, group into companies, cache each company
    (so it shows up in Company Profiles too), then pull recent posts for up
    to max_people people overall (prioritized by seniority tier across the
    whole file) and export one matched CSV — same shape as /api/automate,
    plus a company column since a batch file can span many companies.
    """
    raw = await file.read()
    try:
        people = parse_batch_csv(raw)
    except Exception as e:
        raise HTTPException(400, f"Could not parse CSV: {e}")
    if not people:
        raise HTTPException(400, "No usable rows found — need at least a name or a LinkedIn profile URL per row.")

    groups = group_batch_people(people)

    # Auto-cache every company in the file, same as a live search would.
    for g in groups.values():
        cached_profiles = [
            {k: v for k, v in p.items() if k != "company_url"} for p in g["people"]
        ]
        cache_company(g["company_url"], "csv_import", cached_profiles)

    max_people = max(1, min(max_people, 200))
    posts_per_person = max(1, min(posts_per_person, 15))

    all_people = []
    for g in groups.values():
        for p in g["people"]:
            all_people.append({**p, "_company": g["name"]})
    all_people.sort(key=lambda p: TIER_RANK.get(p.get("tier"), 99))
    selected = all_people[:max_people]

    enriched = []
    for p in selected:
        posts: list[dict] = []
        if p.get("url"):
            try:
                raw_posts = apify_posts(p["url"], posts_per_person)
                posts = [normalize_post(x, "csv_import") for x in (raw_posts or [])]
            except Exception:
                posts = []  # keep the person in the report even if their posts fail
            time.sleep(0.15)
        enriched.append({**p, "posts": posts})

    csv_text = build_batch_csv(enriched)
    log_history("batch", file.filename or "csv_import", {"provider": "csv_import", "count": len(enriched)})
    filename = f"signal_batch_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    return StreamingResponse(
        BytesIO(csv_text.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/intelligence")
def company_intelligence(req: IntelligenceRequest):
    """
    Single-input orchestrator: company name + time filter in, aggregated
    multi-source OSINT + Gemini AI insights out. See run_company_intelligence
    for the tiered collection logic and failsafe behavior. Every completed
    run (partial or full) is cached by company slug so it can be revisited
    instantly later without re-hitting every source.
    """
    if not req.company_name.strip():
        raise HTTPException(400, "company_name is required.")
    max_execs = max(0, min(req.max_execs, 10))
    posts_per_source = max(1, min(req.posts_per_source, 20))
    result = run_company_intelligence(req.company_name.strip(), req.time_filter, max_execs, posts_per_source)
    log_history("intelligence", result["company_name"], {
        "provider": "intelligence",
        "count": result["metrics"]["posts_analyzed"],
    })
    result["slug"] = cache_intelligence_result(result)
    result["cached"] = False
    return result


@app.get("/api/intelligence/cached")
def list_cached_intelligence():
    """Lightweight list of previously run Company Intelligence reports, for
    the 'previously analyzed' grid — no source calls, just the cache index."""
    cache = load_intelligence_cache()
    out = [
        {
            "slug": c["slug"],
            "company_name": c["company_name"],
            "company_logo": c.get("company_logo", ""),
            "time_filter": c.get("time_filter", ""),
            "generated_at": c.get("generated_at", ""),
            "posts_analyzed": c.get("metrics", {}).get("posts_analyzed", 0),
            "trending_interest": c.get("metrics", {}).get("trending_interest", "Unknown"),
        }
        for c in cache.values()
    ]
    out.sort(key=lambda c: c["generated_at"], reverse=True)
    return {"companies": out}


@app.get("/api/intelligence/cached/{slug}")
def get_cached_intelligence(slug: str):
    """Full cached report — instant, no source calls. Use /api/intelligence
    (POST) to run a fresh version and refresh this cache entry."""
    cache = load_intelligence_cache()
    c = cache.get(slug)
    if not c:
        raise HTTPException(404, "No cached report for that company yet — run it fresh first.")
    result = {**c, "cached": True}
    return result


@app.post("/api/intelligence/export")
def company_intelligence_export(req: IntelligenceRequest, fmt: Literal["csv", "json"] = "csv"):
    """Re-runs the pipeline and streams the result as a downloadable file.
    (Kept as a separate call rather than caching runs server-side, since a
    run's freshness matters more than avoiding a repeat Apify hit here — the
    frontend instead exports client-side from the already-fetched result;
    this endpoint exists for direct API/script use.)"""
    max_execs = max(0, min(req.max_execs, 10))
    posts_per_source = max(1, min(req.posts_per_source, 20))
    result = run_company_intelligence(req.company_name.strip(), req.time_filter, max_execs, posts_per_source)
    safe_name = re.sub(r"[^a-z0-9]+", "_", result["company_name"].lower()).strip("_") or "company"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")

    if fmt == "json":
        payload = json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8")
        return StreamingResponse(
            BytesIO(payload), media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="signal_intel_{safe_name}_{stamp}.json"'},
        )

    csv_text = build_intelligence_csv(result)
    return StreamingResponse(
        BytesIO(csv_text.encode("utf-8")), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="signal_intel_{safe_name}_{stamp}.csv"'},
    )


# ----------------------------- pluggable source endpoints ------------------

@app.get("/api/sources")
def list_sources():
    """All configured custom sources, with inlined API keys redacted."""
    return {"sources": [_redact_source_config(c) for c in load_source_configs()]}


@app.post("/api/sources/extract")
def extract_source(req: SourceExtractRequest):
    """Paste API docs -> Gemini returns a draft config (not saved). If an inline
    api_key was provided, bake it into the draft so the follow-up test/save uses it."""
    if not req.docs.strip():
        raise HTTPException(400, "Paste some API documentation first.")
    cfg = extract_source_config_from_docs(req.docs, req.hints)
    if req.api_key.strip():
        auth = cfg.get("auth") or {}
        if not auth.get("type") or auth.get("type") == "none":
            auth["type"] = "bearer"  # sensible default when a key is supplied but docs didn't say how
        auth["key_value"] = req.api_key.strip()
        cfg["auth"] = auth
    problems = validate_source_config(cfg)
    return {"config": cfg, "problems": problems}


@app.post("/api/sources/test")
def test_source(req: SourceTestRequest):
    """Dry-run a config against a sample company and return a few normalized
    items, so you can confirm the mapping works before saving/trusting it."""
    problems = validate_source_config(req.config)
    if problems:
        raise HTTPException(400, "Invalid config: " + "; ".join(problems))
    try:
        handle = _guess_company_handle(req.target)
        target = handle if req.config.get("target_type") == "handle" else req.target
        raw = fetch_config_source(req.config, target, max(1, min(req.max_items, 10)))
        samples = [normalize_config_item(r, req.config) for r in raw]
        return {"ok": True, "count": len(samples), "raw_count": len(raw), "samples": samples[:5]}
    except requests.HTTPError as e:
        return {"ok": False, "error": f"HTTP error: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/sources/debug")
def debug_source(req: SourceDebugRequest):
    """Diagnose why a config's Test call failed — or why it succeeded but
    returned zero items. Fast rule-based checks for common mistakes (missing
    auth key, RapidAPI's dual-header requirement, error-code-specific
    guidance, wrong response_path) plus a Gemini second opinion that, for the
    zero-items case, gets shown the REAL raw API response so it can fix
    response_path/field_map against actual data instead of guessing again.
    If previous_attempts shows this symptom already recurred, both the rule
    layer and Gemini are told explicitly not to repeat a diagnosis that
    already failed to fix it, and to explain why it likely didn't work."""
    problems = validate_source_config(req.config)
    result = debug_source_config(req.config, req.error, req.docs, req.target, req.max_items,
                                 req.zero_items, req.history)
    result["schema_problems"] = problems
    return result


@app.post("/api/sources")
def save_source(req: SourceSaveRequest):
    """Persist a config to sources/. If auth.key_value is the redacted mask
    from a prior list call, keep the existing on-disk key instead of overwriting."""
    cfg = req.config or {}
    incoming_auth = cfg.get("auth") or {}
    if isinstance(incoming_auth, dict) and incoming_auth.get("_key_set") and "…" in str(incoming_auth.get("key_value", "")):
        existing = next((c for c in load_source_configs() if c.get("name") == cfg.get("name")), None)
        if existing and (existing.get("auth") or {}).get("key_value"):
            incoming_auth["key_value"] = existing["auth"]["key_value"]
        incoming_auth.pop("_key_set", None)
        cfg["auth"] = incoming_auth
    saved = save_source_config(cfg)
    return {"saved": True, "config": _redact_source_config(saved)}


@app.post("/api/sources/{name}/toggle")
def toggle_source(name: str):
    cfg = next((c for c in load_source_configs() if c.get("name") == name), None)
    if not cfg:
        raise HTTPException(404, "No such source.")
    cfg["enabled"] = not cfg.get("enabled", True)
    save_source_config(cfg)
    return {"name": name, "enabled": cfg["enabled"]}


@app.delete("/api/sources/{name}")
def remove_source(name: str):
    if not delete_source_config(name):
        raise HTTPException(404, "No such source (or it couldn't be deleted).")
    return {"deleted": True, "name": name}


# ----------------------------- frontend (embedded) -------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Signal Console</title>
<style>
  :root {
    --bg: #0d0d0d; --panel: #171717; --panel-2: #1f1f1f; --line: #333333;
    --ink: #f2f2f2; --ink-dim: #a3a3a3; --ink-faint: #6e6e6e;
    --signal: #f2f2f2; --signal-dim: #7a7a7a; --err: #f2f2f2; --radius: 12px;
    --li: #d0d0d0; --li-glow: rgba(255,255,255,0.14);
    --x: #e7e7e7; --x-bg: #000000; --x-glow: rgba(255,255,255,0.14);
    --intel: #e0e0e0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--ink);
    font-family: "Inter",-apple-system,system-ui,sans-serif; line-height: 1.5; -webkit-font-smoothing: antialiased; }
  .mono { font-family: "SF Mono","JetBrains Mono","Consolas",monospace; }
  a { color: inherit; }

  header {
    border-bottom: 1px solid var(--line); padding: 16px 28px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }
  .brand { display: flex; align-items: center; gap: 11px; cursor: pointer; }
  .brand .pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--signal);
    animation: pulse 2.4s infinite; }
  @keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(255,255,255,0.4);} 70%{box-shadow:0 0 0 7px rgba(255,255,255,0);} 100%{box-shadow:0 0 0 0 rgba(255,255,255,0);} }
  .brand h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .sub { font-size: 11px; color: var(--ink-faint); letter-spacing: 0.05em; text-transform: uppercase; }
  .status { font-size: 12px; color: var(--ink-dim); }
  .status b { color: var(--signal); font-weight: 500; } .status .off { color: var(--ink-faint); }

  main { max-width: 960px; margin: 0 auto; padding: 32px 28px 80px; }

  /* ---------- landing ---------- */
  .landing-title { text-align: center; margin: 30px 0 8px; font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }
  .landing-sub { text-align: center; color: var(--ink-dim); font-size: 14px; margin-bottom: 40px; }
  .platforms { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; max-width: 680px; margin: 0 auto; }
  @media (max-width: 620px) { .platforms { grid-template-columns: 1fr; } }
  .platform {
    position: relative; border-radius: 18px; padding: 40px 28px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel); overflow: hidden;
    transition: transform 0.16s, box-shadow 0.16s, border-color 0.16s;
    display: flex; flex-direction: column; align-items: center; gap: 16px; text-align: center;
  }
  .platform:hover { transform: translateY(-4px); }
  .platform .logo { width: 66px; height: 66px; border-radius: 16px; display: flex; align-items: center; justify-content: center; }
  .platform .pname { font-size: 19px; font-weight: 700; letter-spacing: -0.01em; }
  .platform .pdesc { font-size: 13px; color: var(--ink-dim); }
  .platform.li:hover { border-color: var(--li); box-shadow: 0 12px 40px var(--li-glow); }
  .platform.li .logo { background: var(--li); }
  .platform.x:hover { border-color: #4a4a4a; box-shadow: 0 12px 40px var(--x-glow); }
  .platform.x .logo { background: var(--x-bg); border: 1px solid #333; }
  .platform .go-tag { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-faint); }

  .batch-entry { max-width: 680px; margin: 22px auto 0; display: flex; flex-direction: column; gap: 12px; }
  .batch-card {
    border-radius: 14px; border: 1px dashed var(--line); background: var(--panel);
    padding: 16px 20px; cursor: pointer; display: flex; align-items: center; gap: 14px;
    transition: border-color 0.15s, transform 0.15s;
  }
  .batch-card:hover { border-color: var(--signal-dim); transform: translateY(-2px); }
  .batch-card.intel { border-style: solid; border-color: var(--intel); background: rgba(255,255,255,0.05); }
  .batch-card.intel:hover { box-shadow: 0 8px 28px rgba(255,255,255,0.14); }
  .batch-card .bicon { font-size: 22px; }
  .batch-card .btext { flex: 1; }
  .batch-card .btitle { font-size: 14px; font-weight: 700; }
  .batch-card .bdesc { font-size: 12px; color: var(--ink-dim); margin-top: 2px; }
  .batch-card .go-tag { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-faint); }

  /* ---------- back bar ---------- */
  .backbar { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .back { background: var(--panel); border: 1px solid var(--line); color: var(--ink-dim);
    padding: 8px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; font-family: inherit; }
  .back:hover { color: var(--ink); border-color: var(--ink-faint); }
  .back.automate { color: var(--signal); border-color: var(--signal-dim); font-weight: 600; }
  .back.automate:hover { background: rgba(255,255,255,0.08); }
  .back:disabled { opacity: 0.5; cursor: wait; }
  .viewing-tag { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; }
  .viewing-tag .chip { width: 22px; height: 22px; border-radius: 6px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
  .viewing-tag .chip.li { background: var(--li); } .viewing-tag .chip.x { background: var(--x-bg); border: 1px solid #333; }
  .viewing-tag .chip.intel { background: var(--intel); }
  .viewing-tag img { border-radius: 6px; object-fit: cover; }
  .nav-spacer { margin-left: auto; }

  /* ---------- controls ---------- */
  .control { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 20px; margin-bottom: 24px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
  .field { flex: 1; min-width: 220px; }
  label { display: block; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-faint); margin-bottom: 7px; }
  input[type=text], input[type=number], input[type=file] {
    width: 100%; background: var(--bg); border: 1px solid var(--line); color: var(--ink);
    padding: 11px 13px; border-radius: 8px; font-size: 14px; font-family: inherit; }
  input:focus { outline: none; border-color: var(--signal-dim); }
  input::placeholder { color: var(--ink-faint); }
  input[type=file] { padding: 9px 13px; cursor: pointer; }
  .toggle { display: inline-flex; align-items: center; cursor: pointer; height: 42px; }
  .toggle input { display: none; }
  .toggle .track { width: 44px; height: 24px; border-radius: 12px; background: var(--bg); border: 1px solid var(--line); position: relative; transition: all 0.15s; }
  .toggle .track::after { content:""; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%; background: var(--ink-faint); transition: all 0.15s; }
  .toggle input:checked + .track { background: var(--signal-dim); border-color: var(--signal-dim); }
  .toggle input:checked + .track::after { left: 22px; background: #141414; }
  .go { border: none; font-weight: 600; padding: 12px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; font-family: inherit; white-space: nowrap; }
  .go.li-btn { background: var(--li); color: #fff; } .go.x-btn { background: var(--x-bg); color: var(--x); border: 1px solid #333; }
  .go.automate-btn { background: var(--signal); color: #141414; }
  .go.batch-btn { background: var(--signal); color: #141414; }
  .go.batch-btn.secondary { background: transparent; border: 1px solid var(--signal-dim); color: var(--signal); }
  .go.intel-btn { background: var(--intel); color: #141414; }
  .go:hover { filter: brightness(1.1); } .go:disabled { opacity: 0.5; cursor: not-allowed; }
  .hint { font-size: 12px; color: var(--ink-faint); margin-top: 12px; }

  .radio-row { display: flex; gap: 8px; }
  .radio-pill { position: relative; }
  .radio-pill input { position: absolute; opacity: 0; width: 100%; height: 100%; cursor: pointer; margin: 0; }
  .radio-pill span { display: block; padding: 10px 16px; border: 1px solid var(--line); border-radius: 8px;
    background: var(--bg); color: var(--ink-dim); font-size: 13px; font-weight: 600; white-space: nowrap; transition: all 0.15s; }
  .radio-pill input:checked + span { background: var(--intel); border-color: var(--intel); color: #141414; }

  .results-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--line); }
  .results-head h2 { font-size: 14px; font-weight: 600; } .results-head .meta { font-size: 12px; color: var(--ink-dim); }

  /* ---------- progress bar ---------- */
  .progress-container {
    max-width: 300px;
    margin: 16px auto 0;
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: 8px;
    overflow: hidden;
    height: 8px;
    position: relative;
  }
  .progress-bar {
    height: 100%;
    background: var(--signal);
    width: 30%;
    border-radius: 8px;
    animation: slide 1.5s ease-in-out infinite alternate;
  }
  .progress-bar.intel-bar { background: var(--intel); }
  @keyframes slide {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(300%); }
  }

  /* ---------- people ---------- */
  .person { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 10px; overflow: hidden; transition: border-color 0.15s; }
  .person.open { border-color: var(--signal-dim); }
  .person-main { display: flex; align-items: center; gap: 14px; padding: 14px 18px; cursor: pointer; }
  .person-main:hover { background: var(--panel-2); }
  .avatar { width: 46px; height: 46px; border-radius: 50%; flex: 0 0 auto; background: var(--panel-2);
    border: 1px solid var(--line); object-fit: cover; display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 600; color: var(--ink-faint); }
  .person-id { flex: 1; min-width: 0; }
  .person-id .nm { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .person-id .role { font-size: 13px; color: var(--ink); font-weight: 500; margin-top: 1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .person-id .hl { font-size: 12px; color: var(--ink-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .person-id .loc { font-size: 11px; color: var(--ink-faint); margin-top: 2px; }
  .chevron { color: var(--ink-faint); transition: transform 0.15s; font-size: 12px; }
  .person.open .chevron { transform: rotate(90deg); color: var(--signal); }

  /* tier badges */
  .tier-badge { font-size: 9px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 5px; border: 1px solid var(--line); color: var(--ink-dim); white-space: nowrap; }
  .tier-badge.tier-c-suite { color: #f5f5f5; border-color: #888888; background: rgba(255,255,255,0.14); }
  .tier-badge.tier-vp { color: #d8d8d8; border-color: #6b6b6b; background: rgba(255,255,255,0.10); }
  .tier-badge.tier-director { color: #bdbdbd; border-color: #555555; background: rgba(255,255,255,0.07); }
  .tier-badge.tier-manager { color: #a0a0a0; border-color: #444444; background: rgba(255,255,255,0.04); }
  .tier-badge.tier-individual-contributor { color: var(--ink-faint); }

  /* list/hierarchy toggle */
  .view-toggle { display: flex; align-items: center; gap: 6px; }
  .vt { background: var(--panel); border: 1px solid var(--line); color: var(--ink-dim); font-family: inherit;
    font-size: 12px; padding: 5px 12px; border-radius: 7px; cursor: pointer; }
  .vt:hover { color: var(--ink); }
  .vt.active { background: var(--signal); color: #141414; border-color: var(--signal); font-weight: 600; }

  /* hierarchy view */
  .tier-group { margin-bottom: 22px; }
  .tier-rail { display: flex; align-items: center; gap: 9px; margin-bottom: 10px; padding-bottom: 7px; border-bottom: 1px dashed var(--line); }
  .tier-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--ink-faint); }
  .tier-dot.tier-c-suite { background: #f5f5f5; } .tier-dot.tier-vp { background: #d8d8d8; }
  .tier-dot.tier-director { background: #bdbdbd; } .tier-dot.tier-manager { background: #a0a0a0; }
  .tier-name { font-size: 13px; font-weight: 700; letter-spacing: 0.02em; }
  .tier-count { font-size: 11px; color: var(--ink-faint); background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 1px 8px; }
  .tier-group .tier-cards .person { margin-left: 19px; }

  .detail { border-top: 1px solid var(--line); padding: 18px; background: var(--panel-2); }
  .detail-grid { display: flex; flex-wrap: wrap; gap: 10px 28px; margin-bottom: 16px; }
  .detail-item .lbl { font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-faint); display: block; margin-bottom: 2px; }
  .detail-item .val { color: var(--ink); font-size: 12px; } .detail-item a.val { color: var(--signal); text-decoration: none; }
  .val.muted { color: var(--ink-faint); font-style: italic; }
  .show-posts { background: var(--signal); color: #141414; border: none; font-weight: 600; padding: 9px 18px; border-radius: 8px; cursor: pointer; font-size: 13px; font-family: inherit; }
  .show-posts:hover { filter: brightness(1.08); } .show-posts:disabled { opacity: 0.5; cursor: wait; }

  /* ---------- company profiles page ---------- */
  .company-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
  .company-card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 22px 14px; text-align: center; cursor: pointer; transition: transform 0.15s, border-color 0.15s; }
  .company-card:hover { transform: translateY(-3px); border-color: var(--signal-dim); }
  .company-logo { width: 56px; height: 56px; border-radius: 12px; object-fit: cover; margin: 0 auto 12px; display: block; background: var(--panel-2); border: 1px solid var(--line); }
  .company-logo.placeholder { display: flex; align-items: center; justify-content: center; font-size: 22px; font-weight: 700; color: var(--ink-faint); }
  .company-name { font-size: 14px; font-weight: 600; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .company-meta { font-size: 11px; color: var(--ink-faint); }

  /* ---------- automate panel ---------- */
  .automate-panel { background: var(--panel); border: 1px solid var(--signal-dim); border-radius: var(--radius); padding: 18px 20px; margin-bottom: 22px; }
  .automate-panel .row { align-items: flex-end; }
  .automate-panel .field { flex: 0 0 140px; min-width: 120px; }
  .automate-status { font-size: 12px; color: var(--ink-dim); margin-top: 10px; display: flex; align-items: center; gap: 8px; }
  .spinner { width: 13px; height: 13px; border: 2px solid var(--line); border-top-color: var(--signal); border-radius: 50%; animation: spin 0.7s linear infinite; }
  .spinner.intel-spin { border-top-color: var(--intel); }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---------- batch preview ---------- */
  .batch-summary { font-size: 12px; color: var(--ink-dim); margin-bottom: 14px; }
  .batch-companies { display: flex; flex-direction: column; gap: 8px; margin-bottom: 18px; }
  .batch-co { background: var(--panel); border: 1px solid var(--line); border-radius: 9px; padding: 11px 15px; display: flex; align-items: center; gap: 12px; }
  .batch-co .bc-name { font-size: 13px; font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .batch-co .bc-count { font-size: 11px; color: var(--ink-faint); background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px; padding: 1px 9px; white-space: nowrap; }
  .batch-co .bc-sample { font-size: 11px; color: var(--ink-faint); flex-basis: 100%; margin-top: 2px; }

  /* ---------- coverflow post viewer ---------- */
  .coverflow-wrap { display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 6px; }
  .coverflow-stage { position: relative; width: 100%; max-width: 760px; height: 540px; }
  .coverflow-stage.compact { height: 500px; max-width: 720px; margin: 0 auto; }
  .coverflow-stage.compact .cf-card { width: 500px; height: 480px; }
  .cf-card { position: absolute; top: 50%; left: 50%; width: 500px; max-width: 90vw; height: 500px;
    transition: transform 0.32s ease, opacity 0.32s ease, filter 0.32s ease; cursor: pointer; }
  .cf-card[data-current="1"] { cursor: default; }
  .cf-card .post-card { height: 100%; }
  .viewer-dots { display: flex; justify-content: center; gap: 6px; margin-top: 16px; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--line); transition: all 0.15s; cursor: pointer; }
  .dot.active { background: var(--signal); width: 18px; border-radius: 3px; }

  .post-card { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px; display: flex; flex-direction: column; }
  .post-card .pc-person { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex: 0 0 auto; }
  .post-card .pc-person .avatar { width: 38px; height: 38px; font-size: 13px; }
  .post-card .pc-person-info { min-width: 0; flex: 1; }
  .post-card .pc-name { font-size: 13.5px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .post-card .pc-source { font-size: 10.5px; color: var(--ink-faint); }
  .post-card .ptext { font-size: 15px; white-space: pre-wrap; line-height: 1.55; margin-bottom: 12px; flex: 1; min-height: 0; overflow-y: auto; padding-right: 6px; overscroll-behavior: contain; }
  .post-card .ptext::-webkit-scrollbar { width: 8px; }
  .post-card .ptext::-webkit-scrollbar-thumb { background: var(--line); border-radius: 4px; }
  .post-card .ptext::-webkit-scrollbar-thumb:hover { background: var(--ink-faint); }
  .post-card .ptext { scrollbar-width: thin; scrollbar-color: var(--line) transparent; }
  .post-img { width: 100%; border-radius: 8px; border: 1px solid var(--line); margin-bottom: 12px; background: var(--panel-2); display: block; max-height: 280px; object-fit: cover; }
  .post-foot { display: flex; align-items: center; gap: 14px; font-size: 11px; color: var(--ink-faint); flex-wrap: wrap; }
  .post-foot .eng { color: var(--signal); }
  .open-btn { margin-left: auto; text-decoration: none; font-size: 12px; font-weight: 600; padding: 6px 12px; border-radius: 7px; }
  .open-btn.li { background: var(--li); color: #fff; } .open-btn.x { background: var(--x-bg); color: var(--x); border: 1px solid #333; }

  .empty, .loading, .error { text-align: center; padding: 34px 20px; color: var(--ink-dim); font-size: 14px; }
  .error { color: var(--err); }

  .history { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px 18px; margin-bottom: 22px; }
  .history-head { display: flex; justify-content: space-between; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-faint); margin-bottom: 10px; }
  .history-head button { background: none; border: none; color: var(--ink-faint); font-size: 11px; cursor: pointer; text-transform: uppercase; font-family: inherit; }
  .history-head button:hover { color: var(--err); }
  .hist-item { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--line); cursor: pointer; font-size: 12px; }
  .hist-item:last-child { border-bottom: none; }
  .hist-item:hover .hist-label { color: var(--signal); }
  .hist-kind { font-size: 9px; letter-spacing: 0.05em; text-transform: uppercase; padding: 2px 6px; border-radius: 4px; border: 1px solid var(--line); color: var(--ink-faint); }
  .hist-kind.company { color: var(--li); border-color: #555555; }
  .hist-kind.x { color: #cfcfcf; border-color: #555555; }
  .hist-kind.profile { color: var(--signal); border-color: var(--signal-dim); }
  .hist-kind.batch { color: var(--signal); border-color: var(--signal-dim); }
  .hist-kind.intelligence { color: var(--intel); border-color: #555555; }
  .hist-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-dim); font-family: "SF Mono","Consolas",monospace; }
  .hist-meta { font-size: 11px; color: var(--ink-faint); }

  .arrow { flex: 0 0 auto; width: 44px; height: 44px; border: 1px solid var(--line); background: var(--panel);
    border-radius: 10px; color: var(--ink-dim); font-size: 20px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .arrow:hover:not(:disabled) { background: var(--panel-2); color: var(--ink); }
  .arrow:disabled { opacity: 0.3; cursor: default; }
  .arrow.small { width: 34px; height: 34px; font-size: 16px; border-radius: 8px; }

  /* ---------- Company Intelligence results ---------- */
  .intel-header { display: flex; align-items: center; gap: 16px; background: var(--panel); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 20px 22px; margin-bottom: 18px; }
  .intel-header .ih-icon { width: 52px; height: 52px; border-radius: 12px; background: var(--intel); color: #141414;
    display: flex; align-items: center; justify-content: center; font-size: 22px; font-weight: 800; flex: 0 0 auto; }
  .intel-header .ih-name { font-size: 20px; font-weight: 700; }
  .intel-header .ih-meta { font-size: 12px; color: var(--ink-dim); margin-top: 3px; }

  .metrics-panel { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 18px; }
  .metric-card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .metric-card .mc-value { font-size: 20px; font-weight: 700; color: var(--intel); }
  .metric-card .mc-label { font-size: 11px; color: var(--ink-faint); letter-spacing: 0.04em; text-transform: uppercase; margin-top: 3px; }

  .ai-board { background: var(--panel); border: 1px solid var(--intel); border-radius: var(--radius); padding: 20px 22px; margin-bottom: 22px; }
  .ai-board .ab-head { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 700; color: var(--intel);
    letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 14px; }
  .ai-block { margin-bottom: 14px; }
  .ai-block:last-child { margin-bottom: 0; }
  .ai-block .ab-label { font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-faint); margin-bottom: 5px; }
  .ai-block .ab-text { font-size: 14px; line-height: 1.6; }
  .ai-metrics-list { display: flex; flex-wrap: wrap; gap: 8px; }
  .ai-metric-chip { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px; font-size: 12.5px; }
  .trend-sparkline { display: flex; align-items: flex-end; gap: 2px; height: 70px; margin-top: 12px; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 8px; }
  .trend-bar { flex: 1; min-width: 3px; background: var(--ink-dim); border-radius: 2px 2px 0 0; opacity: 0.85; }
  .trend-bar:hover { background: var(--ink); opacity: 1; }

  .feed-section { margin-bottom: 26px; }
  .feed-persona { margin-bottom: 28px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px 16px 20px; }
  .feed-persona-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px dashed var(--line); }
  .feed-persona-head .fp-tier { font-size: 9px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    padding: 2px 8px; border-radius: 5px; border: 1px solid var(--line); }
  .feed-persona-head .fp-tier.t1 { color: #f5f5f5; border-color: #888888; background: rgba(255,255,255,0.14); }
  .feed-persona-head .fp-tier.t2 { color: #d8d8d8; border-color: #6b6b6b; background: rgba(255,255,255,0.10); }
  .feed-persona-head .fp-name { font-size: 14px; font-weight: 700; }
  .feed-persona-head .fp-count { font-size: 11px; color: var(--ink-faint); }
  .fi-unverified { font-style: italic; color: var(--ink-faint); }

  .intel-errors { font-size: 12px; color: var(--err); background: rgba(255,255,255,0.05); border: 1px solid #4a4a4a;
    border-radius: 8px; padding: 10px 14px; margin-bottom: 18px; }
  .intel-errors summary { cursor: pointer; font-weight: 600; }

  .hidden { display: none !important; }
</style>
</head>
<body>
  <header>
    <div class="brand" onclick="goLanding()">
      <span class="pulse"></span>
      <div><h1>Signal Console</h1></div>
      <span class="sub">Competitive Intelligence</span>
    </div>
    <div class="status" id="status">…</div>
  </header>

  <main>


    <section id="linkedinView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goLanding()">← Back</button>
        <div class="viewing-tag"><span class="chip li"><svg width="12" height="12" viewBox="0 0 24 24" fill="#fff"><path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.13 1.45-2.13 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.63-1.85 3.36-1.85 3.6 0 4.27 2.37 4.27 5.45v6.29zM5.34 7.43a2.07 2.07 0 1 1 0-4.14 2.07 2.07 0 0 1 0 4.14zm1.78 13.02H3.55V9h3.57v11.45zM22.22 0H1.77C.79 0 0 .77 0 1.72v20.56C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.72V1.72C24 .77 23.2 0 22.22 0z"/></svg></span>LinkedIn</div>
        <button class="back automate nav-spacer hidden" id="automateBtnLI" onclick="toggleAutomatePanel('LI')">Automate</button>
        <button class="back" onclick="openCompanyProfiles()">Company Profiles</button>
      </div>
      <div class="automate-panel hidden" id="automatePanelLI">
        <div class="row">
          <div class="field"><label for="autoMaxPeopleLI">People to cover</label>
            <input type="number" id="autoMaxPeopleLI" value="15" min="1" max="50"></div>
          <div class="field"><label for="autoPostsPerLI">Posts per person</label>
            <input type="number" id="autoPostsPerLI" value="5" min="1" max="15"></div>
          <button class="go automate-btn" id="autoRunBtnLI" onclick="runAutomate('LI', liCompanySlug)">Generate CSV</button>
        </div>
        <p class="hint">Builds the tier hierarchy for the company you just searched, pulls recent posts for each person, and matches everything into a single downloadable CSV. One API call per person, so it takes a bit — don't close the tab while it runs.</p>
        <div class="automate-status hidden" id="autoStatusLI"><span class="spinner"></span><span id="autoStatusTextLI">Running…</span></div>
      </div>
      <div class="control">
        <div class="row">
          <div class="field"><label for="company">Company name or LinkedIn URL</label>
            <input type="text" id="company" placeholder="e.g. NetDynamic Consulting  —  or a linkedin.com/company/… URL"></div>
          <div class="field" style="flex:0 0 100px;"><label for="max">Max people</label>
            <input type="number" id="max" value="15" min="1" max="50"></div>
          <div class="field" style="flex:0 0 auto;"><label for="execOnly">Execs only</label>
            <label class="toggle"><input type="checkbox" id="execOnly"><span class="track"></span></label></div>
          <button class="go li-btn" id="run">Find people</button>
        </div>
        <div class="row" style="margin-top:14px;">
          <div class="field"><label for="directProfile">Or: posts directly by profile URL</label>
            <input type="text" id="directProfile" placeholder="https://www.linkedin.com/in/satyanadella/"></div>
          <button class="go li-btn" id="runDirect" style="background:transparent;border:1px solid var(--li);color:var(--li);">Get posts</button>
        </div>
        <p class="hint">Provider: <span id="provLabel">Apify+</span>. Type a company name (LinkedIn is searched automatically) or paste its LinkedIn URL. Every company you look up is auto-saved to Company Profiles. Click a person to see their details, then Show posts.</p>
      </div>
      <div id="liResults"></div>
    </section>


    <section id="companyProfilesView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goBack()">← Back</button>
        <div class="viewing-tag">Company Profiles</div>
      </div>
      <div id="intelCachedGrid"></div>
      <div id="companyGrid"></div>
    </section>

    <section id="companyDetailView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goBack()">← Back</button>
        <div class="viewing-tag" id="cdTag"></div>
        <button class="back automate nav-spacer" id="automateBtn" onclick="toggleAutomatePanel('')">Automate</button>
      </div>
      <div class="automate-panel hidden" id="automatePanel">
        <div class="row">
          <div class="field"><label for="autoMaxPeople">People to cover</label>
            <input type="number" id="autoMaxPeople" value="15" min="1" max="50"></div>
          <div class="field"><label for="autoPostsPer">Posts per person</label>
            <input type="number" id="autoPostsPer" value="5" min="1" max="15"></div>
          <button class="go automate-btn" id="autoRunBtn" onclick="runAutomate('', cdCompany && cdCompany.slug)">Generate CSV</button>
        </div>
        <p class="hint">Builds the tier hierarchy for this company, pulls recent posts for each person (one LinkedIn call per person), and matches everything into a single downloadable CSV (one row per post, tagged with that person's tier/role/contact info). This makes one API call per person, so it takes a bit — don't close the tab while it runs.</p>
        <div class="automate-status hidden" id="autoStatus"><span class="spinner"></span><span id="autoStatusText">Running…</span></div>
      </div>
      <div class="control">
        <div class="row">
          <div class="field"><label for="cdSearch">Search people in this company</label>
            <input type="text" id="cdSearch" placeholder="Type a name…"></div>
        </div>
      </div>
      <div id="cdResults"></div>
    </section>

    <section id="postViewerView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goBack()">← Back</button>
        <div class="viewing-tag" id="pvTag"></div>
        <button class="back nav-spacer" id="exportCsvBtn" onclick="exportPostsCsv()">Export CSV</button>
      </div>
      <div id="pvBody"></div>
    </section>

    <section id="xView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goLanding()">← Back</button>
        <div class="viewing-tag"><span class="chip x"><svg width="11" height="11" viewBox="0 0 24 24" fill="#fff"><path d="M18.9 1.15h3.68l-8.04 9.19L24 22.85h-7.41l-5.8-7.58-6.64 7.58H.47l8.6-9.83L0 1.15h7.6l5.24 6.93 6.06-6.93z"/></svg></span>X / Twitter</div>
      </div>
      <div class="control">
        <div class="row">
          <div class="field"><label for="xHandle">Handle</label>
            <input type="text" id="xHandle" placeholder="@satyanadella  (or just satyanadella)"></div>
          <button class="go x-btn" id="runX">Get tweets</button>
        </div>
        <p class="hint">Pulls recent posts via ScrapeBadger — external X source.</p>
      </div>
      <div class="history hidden" id="historyPanelX"><div class="history-head"><span>Recent</span><button onclick="clearHistory()">Clear</button></div><div id="historyListX"></div></div>
      <div id="xResults"></div>
    </section>

    <section id="batchView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goLanding()">← Back</button>
        <div class="viewing-tag">Batch Import (CSV)</div>
      </div>
      <div class="control">
        <div class="row">
          <div class="field"><label for="batchFile">CSV file (Apollo-style export — Person/Company LinkedIn URL, Title, Seniority…)</label>
            <input type="file" id="batchFile" accept=".csv,text/csv"></div>
        </div>
        <div class="row" style="margin-top:14px;">
          <div class="field" style="flex:0 0 160px;"><label for="batchMaxPeople">Max people (total)</label>
            <input type="number" id="batchMaxPeople" value="30" min="1" max="200"></div>
          <div class="field" style="flex:0 0 160px;"><label for="batchPostsPer">Posts per person</label>
            <input type="number" id="batchPostsPer" value="3" min="1" max="15"></div>
          <button class="go batch-btn secondary" id="batchPreviewBtn" onclick="batchPreview()">Preview companies</button>
          <button class="go batch-btn" id="batchRunBtn" onclick="batchAutomate()" disabled>Run automate → CSV</button>
        </div>
        <p class="hint">Preview first to confirm the companies/people parsed correctly — nothing is sent to LinkedIn yet at that stage. "Run automate" caches every company here (so it shows up in Company Profiles too), pulls recent posts for each person up to your caps, and downloads one combined CSV. This makes one LinkedIn API call per person, so large files take a while — don't close the tab.</p>
        <div class="automate-status hidden" id="batchStatus"><span class="spinner"></span><span id="batchStatusText">Running…</span></div>
      </div>
      <div id="batchResults"></div>
    </section>

    <section id="intelView">
      <div class="backbar">
        <div class="viewing-tag"><span class="chip intel">CI</span>Company Intelligence</div>
        <button class="back nav-spacer" onclick="enterPlatform('linkedin')">LinkedIn</button>
        <button class="back" onclick="enterPlatform('x')">X / Twitter</button>
        <button class="back" onclick="openCompanyProfiles()">Company Profiles</button>
        <button class="back" onclick="navigateTo('batchView')">Batch Import</button>
        <button class="back" onclick="openSources()">Data Sources</button>
      </div>
      <div class="control">
        <div class="row">
          <div class="field"><label for="intelCompany">Company name</label>
            <input type="text" id="intelCompany" placeholder="e.g. Microsoft"></div>
        </div>
        <div class="row" style="margin-top:14px; align-items:center;">
          <div class="field" style="flex:0 0 auto;">
            <label>Time filter</label>
            <div class="radio-row">
              <label class="radio-pill"><input type="radio" name="intelTime" value="1d"><span>1 day</span></label>
              <label class="radio-pill"><input type="radio" name="intelTime" value="1w" checked><span>1 week</span></label>
              <label class="radio-pill"><input type="radio" name="intelTime" value="1m"><span>1 month</span></label>
            </div>
          </div>
          <div class="field" style="flex:0 0 130px;"><label for="intelMaxExecs">Max execs</label>
            <input type="number" id="intelMaxExecs" value="4" min="0" max="10"></div>
          <button class="go intel-btn" id="intelRunBtn" onclick="runIntelligence()">Run Intelligence</button>
        </div>
        <p class="hint">Pulls Tier 1 official sources (company website, LinkedIn page, X, Google Trends) plus Tier 2 named executives, then sends it all to Gemini for a summary. One source or exec failing won't stop the rest — partial results beat a crashed run. Every run is cached, so you can revisit it instantly without spending API calls again.</p>
        <div class="automate-status hidden" id="intelStatus"><span class="spinner intel-spin"></span><span id="intelStatusText">Running…</span></div>
      </div>
      <div id="intelResults"></div>
    </section>

    <section id="sourcesView" class="hidden">
      <div class="backbar">
        <button class="back" onclick="goBack()">← Back</button>
        <div class="viewing-tag"><span class="chip intel">+</span>Data Sources</div>
      </div>
      <div class="control">
        <p class="hint" style="margin:0 0 14px;">Add a new data source without writing code. Paste an API's documentation (or a description of its list endpoint), optionally an API key, and Gemini extracts a config. Test it against a sample company, then save — it runs automatically as a Tier 1 source on every Company Intelligence search. Configs live in the <span class="mono">sources/</span> folder as JSON.</p>
        <div class="row">
          <div class="field"><label for="srcDocs">API documentation / description</label>
            <textarea id="srcDocs" rows="7" placeholder="Paste the docs for the endpoint that lists recent posts/items about a company. Include the URL, method, auth style, and a sample JSON response if you have one." style="width:100%;background:var(--bg);border:1px solid var(--line);color:var(--ink);padding:11px 13px;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical;"></textarea></div>
        </div>
        <div class="row" style="margin-top:12px;">
          <div class="field"><label for="srcHints">Hints (optional)</label>
            <input type="text" id="srcHints" placeholder="e.g. 'the company name goes in the subreddit slot' or 'items are under data.posts'"></div>
          <div class="field" style="flex:0 0 260px;"><label for="srcApiKey">API key (optional — stored in the config file)</label>
            <input type="text" id="srcApiKey" placeholder="paste key if the API needs one"></div>
          <button class="go intel-btn" id="srcExtractBtn" onclick="extractSource()">Extract with Gemini</button>
        </div>
        <div class="automate-status hidden" id="srcStatus"><span class="spinner intel-spin"></span><span id="srcStatusText">Working…</span></div>
      </div>
      <div id="srcDraft"></div>
      <div id="srcList"></div>
    </section>

  </main>

<script>
  const state = { provider: "apify_plus", platform: null };
  let liCompanySlug = null;
  const $ = s => document.querySelector(s);
  const VIEWS = ["linkedinView","xView","companyProfilesView","companyDetailView","postViewerView","batchView","intelView","sourcesView"];

  fetch("/api/health").then(r=>r.json()).then(h=>{
    const a = h.apify_key_present?'<b>Apify ●</b>':'<span class="off">Apify ○</span>';
    const xLabel = h.x_provider ? `X (${h.x_provider}) ●` : "X ○";
    const x = h.x_key_present?`<b>${xLabel}</b>`:'<span class="off">X ○</span>';
    const g = h.gemini_key_present?'<b>Gemini ●</b>':'<span class="off">Gemini ○</span>';
    $("#status").innerHTML = `${a} &nbsp; ${x} &nbsp; ${g}`;
  }).catch(()=>{ $("#status").innerHTML='<span class="off">server unreachable</span>'; });

  function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

  // ---------- navigation ----------
  let navStack = [];
  let currentView = "intelView";
  function showView(viewId){
    VIEWS.forEach(id=>$("#"+id).classList.toggle("hidden", id!==viewId));
    currentView = viewId;
    if(viewId === "companyProfilesView") loadCachedIntelligence();
  }
  function navigateTo(viewId){ navStack.push(currentView); showView(viewId); }
  function goBack(){ showView(navStack.pop() || "intelView"); }
  function goLanding(){ navStack=[]; state.platform=null; showView("intelView"); }
  function enterPlatform(p){
    navStack=["intelView"]; state.platform=p;
    showView(p==="linkedin"?"linkedinView":"xView");
    loadHistory();
  }

  // ---------- LinkedIn: find people ----------
  $("#run").addEventListener("click", ()=>findPeople());
  $("#company").addEventListener("keydown", e=>{ if(e.key==="Enter") findPeople(); });
  $("#runDirect").addEventListener("click", ()=>directPosts());
  $("#directProfile").addEventListener("keydown", e=>{ if(e.key==="Enter") directPosts(); });
  $("#runX").addEventListener("click", ()=>xTweets());
  $("#xHandle").addEventListener("keydown", e=>{ if(e.key==="Enter") xTweets(); });
  $("#intelCompany") && $("#intelCompany").addEventListener("keydown", e=>{ if(e.key==="Enter") runIntelligence(); });
  $("#cdSearch").addEventListener("input", ()=>{
    if(!cdCompany) return;
    const q = $("#cdSearch").value.trim().toLowerCase();
    const filtered = !q ? cdCompany.profiles : cdCompany.profiles.filter(p=>(p.name||"").toLowerCase().includes(q));
    renderCompanyPeople(filtered, "#cdResults");
  });

  async function findPeople(prefill){
    const company=(prefill||$("#company").value).trim();
    if(prefill) $("#company").value=company;
    const box=$("#liResults");
    if(!company){ box.innerHTML='<div class="error">Enter a company name or LinkedIn URL.</div>'; return; }
    const max=parseInt($("#max").value)||15;
    $("#run").disabled=true; 
    box.innerHTML=`
      <div class="loading">
        <div>Scraping key corporate officers from LinkedIn...</div>
        <div class="progress-container"><div class="progress-bar"></div></div>
      </div>`;
    try{
      const r=await fetch("/api/profiles",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({provider:state.provider,company_url:company,max_items:max,exec_only:$("#execOnly").checked})});
      if(!r.ok){const e=await r.json();throw new Error(e.detail||r.statusText);}
      const data=await r.json(); renderPeople(data); loadHistory();
      liCompanySlug = data.slug || null;
      $("#automateBtnLI").classList.toggle("hidden", !liCompanySlug);
      $("#automatePanelLI").classList.add("hidden");
      $("#autoStatusLI").classList.add("hidden");
    }catch(err){ box.innerHTML='<div class="error">Could not load people: '+esc(err.message)+'</div>'; }
    finally{ $("#run").disabled=false; }
  }

  function tierBadge(tier){
    if(!tier) return "";
    const cls = "tier-"+tier.toLowerCase().replace(/[^a-z]+/g,"-").replace(/^-|-$/g,"");
    return `<span class="tier-badge ${cls}">${esc(tier)}</span>`;
  }

  function personCardHTML(p, i){
    const initials=(p.name||"?").split(" ").map(w=>w[0]).slice(0,2).join("").toUpperCase();
    const av=p.photo?`<img class="avatar" src="${esc(p.photo)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar',textContent:'${esc(initials)}'}))">`:`<div class="avatar">${esc(initials)}</div>`;
    const role = p.position || p.headline || "";
    return `<div class="person" data-i="${i}">
      <div class="person-main">${av}
        <div class="person-id">
          <div class="nm">${esc(p.name)} ${tierBadge(p.tier)}</div>
          ${role?`<div class="role">${esc(role)}</div>`:""}
          ${p.headline && p.headline!==role?`<div class="hl">${esc(p.headline)}</div>`:""}
          ${p.location?`<div class="loc">${esc(p.location)}</div>`:""}
        </div>
        <span class="chevron">▶</span></div>
      <div class="detail hidden">
        <div class="detail-grid">
          <div class="detail-item"><span class="lbl">Role</span><span class="val">${esc(p.position||p.headline||"—")}</span></div>
          <div class="detail-item"><span class="lbl">Seniority</span><span class="val">${esc(p.tier||"—")}</span></div>
          <div class="detail-item"><span class="lbl">Email</span>${p.email?`<a class="val" href="mailto:${esc(p.email)}">${esc(p.email)}</a>`:`<span class="val muted">not in this tier</span>`}</div>
          <div class="detail-item"><span class="lbl">Location</span><span class="val">${esc(p.location||"—")}</span></div>
          <div class="detail-item"><span class="lbl">Profile</span>${p.url?`<a class="val" href="${esc(p.url)}" target="_blank">view ↗</a>`:`<span class="val muted">—</span>`}</div>
        </div>
        <button class="show-posts" data-url="${esc(p.url)}" data-urn="${esc(p.urn)}" data-name="${esc(p.name)}">Show posts</button>
      </div></div>`;
  }

  function wirePersonCards(box){
    box.querySelectorAll(".person-main").forEach(m=>m.addEventListener("click",()=>{
      const person=m.closest(".person"), d=person.querySelector(".detail");
      const open=!d.classList.contains("hidden"); d.classList.toggle("hidden",open); person.classList.toggle("open",!open);
    }));
    box.querySelectorAll(".show-posts").forEach(b=>b.addEventListener("click",(e)=>{
      e.stopPropagation();
      goShowPosts(b.dataset.url, b.dataset.urn, b.dataset.name, "linkedin");
    }));
  }

  const TIER_ORDER = ["C-Suite","VP","Director","Manager","Individual Contributor"];
  const peopleViewMode = {};

  function renderPeople(data){
    const box=$("#liResults");
    if(!data.count && !Object.keys(data.errors||{}).length){ box.innerHTML='<div class="empty">No people returned.</div>'; return; }
    const err=renderErrors(data.errors);
    const meta=`via ${esc(data.provider)} · ${data.elapsed_sec}s`;
    renderPeopleInto("#liResults", data.profiles, meta, err);
  }

  function renderPeopleInto(selector, profiles, metaHtml, errHtml){
    const box = $(selector);
    errHtml = errHtml || "";
    if(!profiles || !profiles.length){ box.innerHTML=errHtml+'<div class="empty">No matching people.</div>'; return; }
    const mode = peopleViewMode[selector] || "list";
    const head = `<div class="results-head">
        <h2>${profiles.length} people</h2>
        <div class="view-toggle">
          <button class="vt ${mode==='list'?'active':''}" data-mode="list">List</button>
          <button class="vt ${mode==='tree'?'active':''}" data-mode="tree">Hierarchy</button>
          ${metaHtml?`<span class="meta" style="margin-left:12px">${metaHtml}</span>`:""}
        </div>
      </div>`;
    const bodyHtml = mode==="tree" ? hierarchyHTML(profiles) : profiles.map((p,i)=>personCardHTML(p,i)).join("");
    box.innerHTML = errHtml + head + `<div class="people-body">${bodyHtml}</div>`;
    wirePersonCards(box);
    box.querySelectorAll(".vt").forEach(b=>b.addEventListener("click",()=>{
      peopleViewMode[selector] = b.dataset.mode;
      renderPeopleInto(selector, profiles, metaHtml, errHtml);
    }));
  }

  function hierarchyHTML(profiles){
    const byTier = {};
    profiles.forEach((p,i)=>{ const t=p.tier||"Individual Contributor"; (byTier[t]=byTier[t]||[]).push({p,i}); });
    return TIER_ORDER.filter(t=>byTier[t]&&byTier[t].length).map(t=>{
      const cards = byTier[t].map(({p,i})=>personCardHTML(p,i)).join("");
      const cls = 'tier-'+t.toLowerCase().replace(/[^a-z]+/g,'-').replace(/^-|-$/g,'');
      return `<div class="tier-group">
        <div class="tier-rail"><span class="tier-dot ${cls}"></span><span class="tier-name">${esc(t)}</span><span class="tier-count">${byTier[t].length}</span></div>
        <div class="tier-cards">${cards}</div>
      </div>`;
    }).join("");
  }

  // ---------- direct LinkedIn posts ----------
  async function directPosts(prefill){
    const url=(prefill||$("#directProfile").value).trim();
    if(prefill) $("#directProfile").value=url;
    if(!url){ $("#liResults").innerHTML='<div class="error">Paste a profile URL.</div>'; return; }
    $("#runDirect").disabled=true;
    navigateTo("postViewerView");
    $("#pvTag").innerHTML = `<span class="chip li"></span>${esc(url)}`;
    $("#pvBody").innerHTML = '<div class="loading">Fetching posts…</div>';
    try{
      const r=await fetch("/api/posts",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({provider:state.provider,profile_url:url,max_items:12})});
      if(!r.ok){const e=await r.json();throw new Error(e.detail||r.statusText);}
      const data=await r.json();
      renderPostViewer(data.posts, "linkedin", url);
      loadHistory();
    }catch(err){ $("#pvBody").innerHTML='<div class="error">Could not load posts: '+esc(err.message)+'</div>'; }
    finally{ $("#runDirect").disabled=false; }
  }

  // ---------- X tweets ----------
  async function xTweets(prefill){
    const handle=(prefill||$("#xHandle").value).trim().replace(/^@/,"");
    if(prefill) $("#xHandle").value=handle;
    const box=$("#xResults");
    if(!handle){ box.innerHTML='<div class="error">Enter a handle.</div>'; return; }
    $("#runX").disabled=true; box.innerHTML='<div class="loading">Fetching @'+esc(handle)+'…</div>';
    try{
      const r=await fetch("/api/x",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({handle,max_items:20})});
      if(!r.ok){const e=await r.json();throw new Error(e.detail||r.statusText);}
      const data=await r.json();
      box.innerHTML='';
      navigateTo("postViewerView");
      renderPostViewer(data.posts, "x", "@"+handle);
      loadHistory();
    }catch(err){ box.innerHTML='<div class="error">Could not load tweets: '+esc(err.message)+'</div>'; }
    finally{ $("#runX").disabled=false; }
  }

  // ---------- show posts (full-page coverflow) ----------
  async function goShowPosts(url, urn, name, platform){
    navigateTo("postViewerView");
    $("#pvTag").textContent = name || "Posts";
    $("#pvBody").innerHTML = '<div class="loading">Loading posts…</div>';
    try{
      const r=await fetch("/api/posts",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({provider:state.provider,profile_url:url,profile_urn:urn,max_items:12})});
      if(!r.ok){const e=await r.json();throw new Error(e.detail||r.statusText);}
      const data=await r.json();
      renderPostViewer(data.posts, platform, name || url);
    }catch(err){ $("#pvBody").innerHTML='<div class="error">'+esc(err.message)+'</div>'; }
  }

  // ---------- coverflow post viewer ----------
  let currentPV = { posts: [], platform: "linkedin", idx: 0, label: "" };

  document.addEventListener("keydown", e=>{
    if(currentView!=="postViewerView") return;
    if(!currentPV.posts.length) return;
    if(e.key==="ArrowLeft"){ e.preventDefault(); movePV(-1); }
    else if(e.key==="ArrowRight"){ e.preventDefault(); movePV(1); }
  });

  function renderPostViewer(posts, platform, headerLabel){
    currentPV = { posts: posts||[], platform, idx: 0, label: headerLabel||"" };
    $("#pvTag").innerHTML = `<span class="chip ${platform==='x'?'x':'li'}"></span>${esc(headerLabel||"")}`;
    const body = $("#pvBody");
    const exportBtn = $("#exportCsvBtn");
    if(exportBtn) exportBtn.classList.toggle("hidden", !(posts && posts.length));
    if(!posts || !posts.length){ body.innerHTML='<div class="empty">No posts found.</div>'; return; }
    body.innerHTML = `
      <div class="coverflow-wrap">
        <button class="arrow cf-prev">‹</button>
        <div class="coverflow-stage" id="cfStage"></div>
        <button class="arrow cf-next">›</button>
      </div>
      <div class="viewer-dots" id="cfDots"></div>`;
    body.querySelector(".cf-prev").addEventListener("click", ()=>movePV(-1));
    body.querySelector(".cf-next").addEventListener("click", ()=>movePV(1));
    buildCoverflow();
  }

  function cfTransform(offset){
    const dir = offset<0?-1:1, a=Math.abs(offset);
    let tx=0, scale=1, z=5, op=1, blur=0, display="block";
    if(a===0){ tx=0; scale=1; z=5; op=1; }
    else if(a===1){ tx=58*dir; scale=0.8; z=4; op=0.6; blur=1; }
    else if(a===2){ tx=100*dir; scale=0.64; z=3; op=0.25; blur=2; }
    else { display="none"; }
    return `display:${display}; transform:translate(-50%,-50%) translateX(${tx}%) scale(${scale}); z-index:${z}; opacity:${op}; filter:blur(${blur}px);`;
  }

  function buildCoverflow(){
    const {posts, platform, idx} = currentPV;
    const stage = $("#cfStage");
    if(!stage) return;
    const openLbl = platform==="x" ? "Open on X" : "Open on LinkedIn";
    stage.innerHTML = posts.map((p,i)=>{
      const offset = i-idx;
      if(Math.abs(offset) > 2) return "";
      const img = (p.images&&p.images.length)?`<img class="post-img" src="${esc(p.images[0])}" loading="lazy" onerror="this.remove()">`:"";
      return `<div class="cf-card" data-i="${i}" data-current="${i===idx?1:0}" style="${cfTransform(offset)}">
        <div class="post-card">
          ${img}
          <div class="ptext">${esc(p.text)||"<span style='color:var(--ink-faint)'>(no text)</span>"}</div>
          <div class="post-foot">
            <span>${esc(p.date||"—")}</span><span class="eng">${p.reactions||0} likes</span><span>${p.comments||0} comments</span>
            ${p.url?`<a class="open-btn ${platform==='x'?'x':'li'}" href="${esc(p.url)}" target="_blank">${openLbl} ↗</a>`:""}
          </div>
        </div></div>`;
    }).join("");
    stage.querySelectorAll(".cf-card").forEach(el=>el.addEventListener("click",(e)=>{
      if(e.target.closest(".open-btn")) return;
      const i=parseInt(el.dataset.i);
      if(i!==currentPV.idx){ currentPV.idx=i; buildCoverflow(); }
    }));
    updatePVControls();
  }

  function movePV(delta){
    const {posts} = currentPV;
    currentPV.idx = Math.max(0, Math.min(posts.length-1, currentPV.idx+delta));
    buildCoverflow();
  }

  function updatePVControls(){
    const {posts, idx} = currentPV;
    const prev=document.querySelector(".cf-prev"), next=document.querySelector(".cf-next");
    if(prev) prev.disabled = idx<=0;
    if(next) next.disabled = idx>=posts.length-1;
    const dots = $("#cfDots");
    if(dots){
      dots.innerHTML = posts.map((_,i)=>`<span class="dot ${i===idx?'active':''}" data-i="${i}"></span>`).join("");
      dots.querySelectorAll(".dot").forEach(d=>d.addEventListener("click",()=>{
        currentPV.idx = parseInt(d.dataset.i); buildCoverflow();
      }));
    }
  }

  // ---------- CSV export ----------
  function csvCell(v){
    const s = (v==null?"":String(v));
    return '"' + s.replace(/"/g,'""') + '"';
  }
  function exportPostsCsv(){
    const {posts, platform, label} = currentPV;
    if(!posts || !posts.length) return;
    const cols = ["index","platform","date","text","reactions","comments","images","url"];
    const rows = [cols.join(",")];
    posts.forEach((p,i)=>{
      rows.push([
        csvCell(i+1),
        csvCell(platform),
        csvCell(p.date),
        csvCell(p.text),
        csvCell(p.reactions||0),
        csvCell(p.comments||0),
        csvCell((p.images||[]).join(" | ")),
        csvCell(p.url),
      ].join(","));
    });
    const csv = "\ufeff" + rows.join("\r\n");
    const blob = new Blob([csv], {type:"text/csv;charset=utf-8;"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const safe = (label||"posts").replace(/[^a-z0-9_@-]+/gi,"_").replace(/^_+|_+$/g,"") || "posts";
    const stamp = new Date().toISOString().slice(0,10);
    a.href = url; a.download = `signal_${platform}_${safe}_${stamp}.csv`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  }

  // ---------- Company Profiles page ----------
  async function openCompanyProfiles(){
    navigateTo("companyProfilesView");
    const grid = $("#companyGrid");
    grid.innerHTML = '<div class="loading">Loading companies…</div>';
    try{
      const data = await (await fetch("/api/companies")).json();
      if(!data.companies.length){ grid.innerHTML = '<div class="empty">No companies searched yet. Go to LinkedIn and find people at a company first — it\'ll show up here automatically.</div>'; return; }
      grid.innerHTML = `<div class="company-grid">` + data.companies.map(c=>{
        const initial = esc((c.name||"?")[0].toUpperCase());
        const logo = c.logo
          ? `<img class="company-logo" src="${esc(c.logo)}" alt="" onerror="this.outerHTML='<div class=\\'company-logo placeholder\\'>${initial}</div>'">`
          : `<div class="company-logo placeholder">${initial}</div>`;
        return `<div class="company-card" data-slug="${esc(c.slug)}">
          ${logo}
          <div class="company-name">${esc(c.name)}</div>
          <div class="company-meta">${c.count} cached</div>
        </div>`;
      }).join("") + `</div>`;
      grid.querySelectorAll(".company-card").forEach(el=>el.addEventListener("click",()=>openCompanyDetail(el.dataset.slug)));
    }catch(err){ grid.innerHTML = '<div class="error">Could not load companies.</div>'; }
  }

  let cdCompany = null;
  async function openCompanyDetail(slug){
    navigateTo("companyDetailView");
    $("#cdSearch").value = "";
    $("#autoStatus").classList.add("hidden");
    $("#automatePanel").classList.add("hidden");
    $("#cdResults").innerHTML = '<div class="loading">Loading…</div>';
    try{
      const c = await (await fetch(`/api/companies/${encodeURIComponent(slug)}`)).json();
      cdCompany = c;
      const logoImg = c.logo ? `<img src="${esc(c.logo)}" style="width:22px;height:22px;border-radius:6px;object-fit:cover" onerror="this.remove()">` : "";
      $("#cdTag").innerHTML = `${logoImg} ${esc(c.name)}`;
      renderCompanyPeople(c.profiles, "#cdResults");
    }catch(err){ $("#cdResults").innerHTML='<div class="error">Could not load this company.</div>'; }
  }

  function renderCompanyPeople(profiles, selector){
    renderPeopleInto(selector, profiles, "", "");
  }

  // ---------- Automate: hierarchy + posts + CSV ----------
  function automateIds(suffix){
    return {
      panel: "#automatePanel"+suffix,
      maxPeople: "#autoMaxPeople"+suffix,
      postsPer: "#autoPostsPer"+suffix,
      runBtn: "#autoRunBtn"+suffix,
      toggleBtn: "#automateBtn"+suffix,
      status: "#autoStatus"+suffix,
      statusText: "#autoStatusText"+suffix,
    };
  }

  function toggleAutomatePanel(suffix){
    $(automateIds(suffix).panel).classList.toggle("hidden");
  }

  async function runAutomate(suffix, slug){
    if(!slug) return;
    const ids = automateIds(suffix);
    const maxPeople = parseInt($(ids.maxPeople).value) || 15;
    const postsPer = parseInt($(ids.postsPer).value) || 5;
    const btn = $(ids.runBtn), toggleBtn = $(ids.toggleBtn);
    const status = $(ids.status), statusText = $(ids.statusText);
    btn.disabled = true;
    toggleBtn.disabled = true;
    status.classList.remove("hidden");
    statusText.textContent = `Pulling posts for up to ${maxPeople} people (~${maxPeople} LinkedIn calls) — this can take a few minutes…`;
    try{
      const r = await fetch("/api/automate", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({slug, max_people: maxPeople, posts_per_person: postsPer})});
      if(!r.ok){
        let msg = r.statusText;
        try{ const e = await r.json(); msg = e.detail || msg; }catch{}
        throw new Error(msg);
      }
      const blob = await r.blob();
      const disposition = r.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : `signal_${slug}.csv`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a); URL.revokeObjectURL(url);
      statusText.textContent = "Done — CSV downloaded.";
    }catch(err){
      statusText.textContent = "Failed: " + err.message;
    }finally{
      btn.disabled = false;
      toggleBtn.disabled = false;
    }
  }

  function renderErrors(errors){
    if(!errors||!Object.keys(errors).length) return "";
    const lines=Object.entries(errors).map(([p,m])=>`<div><b>${esc(p)}</b>: ${esc(m)}</div>`).join("");
    return `<div style="font-size:12px;color:var(--err);background:rgba(255,255,255,0.05);border:1px solid #4a4a4a;border-radius:8px;padding:10px 14px;margin-bottom:14px;">${lines}</div>`;
  }

  // ---------- Batch import (CSV) ----------
  let batchFileCache = null;   // File object the user picked, reused across preview + automate
  let batchLastPreview = null;

  $("#batchFile") && $("#batchFile").addEventListener("change", ()=>{
    const f = $("#batchFile").files[0] || null;
    batchFileCache = f;
    $("#batchRunBtn").disabled = true;
    batchLastPreview = null;
    $("#batchResults").innerHTML = "";
  });

  async function batchPreview(){
    const box = $("#batchResults");
    const f = $("#batchFile").files[0];
    if(!f){ box.innerHTML = '<div class="error">Choose a CSV file first.</div>'; return; }
    batchFileCache = f;
    $("#batchPreviewBtn").disabled = true;
    box.innerHTML = '<div class="loading">Reading CSV…</div>';
    try{
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch("/api/batch/preview", {method:"POST", body: fd});
      if(!r.ok){ const e = await r.json(); throw new Error(e.detail || r.statusText); }
      const data = await r.json();
      batchLastPreview = data;
      renderBatchPreview(data);
      $("#batchRunBtn").disabled = false;
    }catch(err){
      box.innerHTML = '<div class="error">Could not read CSV: '+esc(err.message)+'</div>';
      $("#batchRunBtn").disabled = true;
    }finally{
      $("#batchPreviewBtn").disabled = false;
    }
  }

  function renderBatchPreview(data){
    const box = $("#batchResults");
    if(!data.companies.length){ box.innerHTML = '<div class="empty">No companies found in that file.</div>'; return; }
    const summary = `<div class="batch-summary">${data.total_rows} usable rows → ${data.company_count} companies. Check the names below match what you expect, then run automate.</div>`;
    const list = data.companies.map(c=>`
      <div class="batch-co">
        <div class="bc-name">${esc(c.name)}</div>
        <div class="bc-count">${c.count} people${c.with_linkedin_url<c.count?` · ${c.with_linkedin_url} with a profile URL`:""}</div>
        <div class="bc-sample">e.g. ${esc(c.sample_names.join(", "))}</div>
      </div>`).join("");
    box.innerHTML = summary + `<div class="batch-companies">${list}</div>`;
  }

  async function batchAutomate(){
    const f = $("#batchFile").files[0] || batchFileCache;
    if(!f){ $("#batchResults").innerHTML = '<div class="error">Choose a CSV file first.</div>'; return; }
    const maxPeople = parseInt($("#batchMaxPeople").value) || 30;
    const postsPer = parseInt($("#batchPostsPer").value) || 3;
    const runBtn = $("#batchRunBtn"), previewBtn = $("#batchPreviewBtn");
    const status = $("#batchStatus"), statusText = $("#batchStatusText");
    runBtn.disabled = true; previewBtn.disabled = true;
    status.classList.remove("hidden");
    statusText.textContent = `Caching companies and pulling posts for up to ${maxPeople} people (~${maxPeople} LinkedIn calls) — this can take a while…`;
    try{
      const fd = new FormData();
      fd.append("file", f);
      fd.append("max_people", maxPeople);
      fd.append("posts_per_person", postsPer);
      const r = await fetch("/api/batch/automate", {method:"POST", body: fd});
      if(!r.ok){
        let msg = r.statusText;
        try{ const e = await r.json(); msg = e.detail || msg; }catch{}
        throw new Error(msg);
      }
      const blob = await r.blob();
      const disposition = r.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : `signal_batch.csv`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a); URL.revokeObjectURL(url);
      statusText.textContent = "Done — CSV downloaded. Companies from this file were also cached into Company Profiles.";
    }catch(err){
      statusText.textContent = "Failed: " + err.message;
    }finally{
      runBtn.disabled = false; previewBtn.disabled = false;
    }
  }

  // ---------- Company Intelligence ----------
  let intelLastResult = null;

  function getIntelTimeFilter(){
    const el = document.querySelector('input[name="intelTime"]:checked');
    return el ? el.value : "1w";
  }

  async function loadCachedIntelligence(){
    const grid = $("#intelCachedGrid");
    if(!grid) return;
    try{
      const data = await (await fetch("/api/intelligence/cached")).json();
      if(!data.companies || !data.companies.length){ grid.innerHTML = ""; return; }
      grid.innerHTML = `
        <div class="results-head"><h2>Previously Analyzed</h2><span class="meta">Loads instantly — no new API calls</span></div>
        <div class="company-grid">` + data.companies.map(c=>{
          const initial = esc((c.company_name||"?")[0].toUpperCase());
          const when = c.generated_at ? new Date(c.generated_at).toLocaleDateString() : "";
          const logo = c.company_logo
            ? `<img class="company-logo" src="${esc(c.company_logo)}" alt="" onerror="this.outerHTML='<div class=\\'company-logo placeholder\\'>${initial}</div>'">`
            : `<div class="company-logo placeholder">${initial}</div>`;
          return `<div class="company-card" data-slug="${esc(c.slug)}">
            ${logo}
            <div class="company-name">${esc(c.company_name)}</div>
            <div class="company-meta">${esc(c.trending_interest||"")} interest · ${when}</div>
          </div>`;
        }).join("") + `</div>`;
      grid.querySelectorAll(".company-card").forEach(el=>el.addEventListener("click",()=>openCachedIntelligence(el.dataset.slug)));
    }catch(err){ grid.innerHTML = ""; }
  }

  async function openCachedIntelligence(slug){
    navigateTo("intelView");
    const box = $("#intelResults");
    box.innerHTML = '<div class="loading">Loading cached report…</div>';
    try{
      const r = await fetch(`/api/intelligence/cached/${encodeURIComponent(slug)}`);
      if(!r.ok){ const e = await r.json(); throw new Error(e.detail || r.statusText); }
      const data = await r.json();
      intelLastResult = data;
      $("#intelCompany").value = data.company_name;
      renderIntelligence(data);
      box.scrollIntoView({behavior:"smooth", block:"start"});
    }catch(err){
      box.innerHTML = '<div class="error">Could not load cached report: '+esc(err.message)+'</div>';
    }
  }

  async function runIntelligence(){
    const company = $("#intelCompany").value.trim();
    const box = $("#intelResults");
    if(!company){ box.innerHTML = '<div class="error">Enter a company name.</div>'; return; }
    const timeFilter = getIntelTimeFilter();
    const maxExecs = parseInt($("#intelMaxExecs").value);
    const btn = $("#intelRunBtn");
    const status = $("#intelStatus"), statusText = $("#intelStatusText");
    btn.disabled = true;
    status.classList.remove("hidden");
    statusText.textContent = `Collecting Tier 1 official sources, then Tier 2 executives for "${company}" — this can take a couple minutes…`;
    box.innerHTML = `<div class="loading"><div>Orchestrating sources…</div><div class="progress-container"><div class="progress-bar intel-bar"></div></div></div>`;
    try{
      const r = await fetch("/api/intelligence", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({company_name: company, time_filter: timeFilter, max_execs: isNaN(maxExecs)?4:maxExecs, posts_per_source: 8})});
      if(!r.ok){ const e = await r.json(); throw new Error(e.detail || r.statusText); }
      const data = await r.json();
      intelLastResult = data;
      renderIntelligence(data);
      statusText.textContent = `Done in ${data.elapsed_sec}s. Cached for next time.`;
      loadCachedIntelligence();
    }catch(err){
      box.innerHTML = '<div class="error">Could not run intelligence: '+esc(err.message)+'</div>';
      statusText.textContent = "Failed.";
    }finally{
      btn.disabled = false;
    }
  }

  function sourceLabel(source){
    return {linkedin:"LinkedIn", x:"X", website:"Website"}[source] || source;
  }

  function renderIntelligence(data){
    const box = $("#intelResults");
    const initial = esc((data.company_name||"?")[0].toUpperCase());
    const errCount = Object.keys(data.errors||{}).length;

    const cachedBanner = data.cached ? `
      <div class="intel-errors" style="color:var(--intel);background:rgba(255,255,255,0.05);border-color:#4a4a4a;display:flex;align-items:center;gap:10px;justify-content:space-between;">
        <span>Showing a cached report from ${new Date(data.generated_at).toLocaleString()}.</span>
        <button class="go intel-btn" style="padding:7px 14px;font-size:12px;" onclick="runIntelligence()">Run Fresh</button>
      </div>` : "";

    const headerLogo = data.company_logo
      ? `<img class="ih-icon" style="object-fit:cover;" src="${esc(data.company_logo)}" alt="" onerror="this.outerHTML='<div class=\\'ih-icon\\'>${initial}</div>'">`
      : `<div class="ih-icon">${initial}</div>`;
    const header = `
      <div class="intel-header">
        ${headerLogo}
        <div>
          <div class="ih-name">${esc(data.company_name)}</div>
          <div class="ih-meta">Time filter: ${esc(data.time_filter)} &nbsp;·&nbsp; Generated ${new Date(data.generated_at).toLocaleString()} &nbsp;·&nbsp; ${data.elapsed_sec}s</div>
        </div>
      </div>`;

    const m = data.metrics || {};
    const metricsPanel = `
      <div class="metrics-panel">
        <div class="metric-card"><div class="mc-value">${m.posts_analyzed||0}</div><div class="mc-label">Posts analyzed</div></div>
        <div class="metric-card"><div class="mc-value">${(m.sources_covered||[]).length}</div><div class="mc-label">Sources covered</div></div>
        <div class="metric-card"><div class="mc-value">${m.execs_covered||0}</div><div class="mc-label">Execs covered</div></div>
        <div class="metric-card"><div class="mc-value">${esc(m.trending_interest||"Unknown")}</div><div class="mc-label">Trending interest</div></div>
      </div>`;

    const ai = data.ai_insights || {};
    const metricsChips = (ai.metrics && ai.metrics.length)
      ? `<div class="ai-metrics-list">${ai.metrics.map(x=>`<span class="ai-metric-chip">${esc(x)}</span>`).join("")}</div>`
      : `<div class="ab-text" style="color:var(--ink-faint);font-style:italic;">No explicit metrics found in the collected content.</div>`;
    const aiBoard = `
      <div class="ai-board">
        <div class="ab-head">AI Insights (Gemini)</div>
        <div class="ai-block"><div class="ab-label">Summary</div><div class="ab-text">${esc(ai.summary || "Not available.")}</div></div>
        <div class="ai-block"><div class="ab-label">Metrics &amp; financial highlights</div>${metricsChips}</div>
        <div class="ai-block"><div class="ab-label">Sentiment</div><div class="ab-text">${esc(ai.sentiment || "Not available.")}</div></div>
        <div class="ai-block"><div class="ab-label">Strategic insights</div><div class="ab-text">${esc(ai.strategy_insights || "Not available.")}</div></div>
      </div>`;

    // Merge every persona's posts into one feed, newest first — used by the
    // "Newest First" grouping mode. Website text isn't a "post" (no
    // author/date/engagement) so it stays out of the feed entirely.
    function parseDateForSort(s){
      if(!s) return null;
      const t = new Date(s).getTime();
      return isNaN(t) ? null : t;
    }
    let merged = [];
    (data.feed || []).forEach(group=>{
      group.items.forEach(it=>{
        if(it.source === "website") return;
        merged.push(Object.assign({}, it, {persona: group.persona, tier: group.tier}));
      });
    });
    merged.sort((a,b)=>{
      const da = parseDateForSort(a.date), db = parseDateForSort(b.date);
      if(da===null && db===null) return 0;
      if(da===null) return 1;   // posts with an unparseable date sort last
      if(db===null) return -1;
      return db - da;           // newest first
    });

    window.__feedGroupMode = "newest";
    window.__intelFeedData = data;
    window.__intelFeedMerged = merged;

    const feedSection = `<div class="feed-section"><div class="results-head"><h2>Aggregated Feed</h2>
      <div class="view-toggle">
        <button class="vt fg-btn active" data-mode="newest">Newest First</button>
        <button class="vt fg-btn" data-mode="persona">By Persona</button>
        <button class="vt fg-btn" data-mode="tier">By Tier</button>
        <span class="meta" style="margin-left:12px">${merged.length} post${merged.length===1?"":"s"}</span>
        <button class="back" style="margin-left:10px" onclick="exportIntelligence('csv')">CSV</button>
        <button class="back" style="margin-left:6px" onclick="exportIntelligence('json')">JSON</button>
      </div>
      </div>
      <div id="feedBody"></div>
      </div>`;

    const errBlock = errCount ? `
      <details class="intel-errors"><summary>${errCount} source${errCount===1?"":"s"} had issues (partial data collected — see below)</summary>
        ${Object.entries(data.errors).map(([k,v])=>`<div style="margin-top:6px;"><b>${esc(k)}</b>: ${esc(v)}</div>`).join("")}
      </details>` : "";

    const trends = data.trends || {};
    const points = trends.points || [];
    const sparkline = points.length ? `
      <div class="trend-sparkline">
        ${points.map(p=>`<div class="trend-bar" style="height:${Math.max(4, (Number(p.value)||0))}%" title="${esc(p.time)}: ${esc(p.value)}"></div>`).join("")}
      </div>` : "";
    const relatedTop = (trends.related_queries && trends.related_queries.top) || [];
    const relatedRising = (trends.related_queries && trends.related_queries.rising) || [];
    const relatedChips = (relatedTop.length || relatedRising.length) ? `
      <div class="ai-block"><div class="ab-label">Related searches</div>
        <div class="ai-metrics-list">
          ${relatedTop.slice(0,5).map(q=>`<span class="ai-metric-chip">${esc(q.query||q.title||"")}</span>`).join("")}
          ${relatedRising.slice(0,5).map(q=>`<span class="ai-metric-chip" style="border-style:dashed;">${esc(q.query||q.title||"")} \u2191</span>`).join("")}
        </div>
      </div>` : "";
    const trendsSection = trends.keyword ? `
      <div class="ai-board" style="margin-bottom:22px;">
        <div class="ab-head">Google Trends</div>
        <div class="ai-block">
          <div class="ab-label">Search interest for "${esc(trends.keyword)}" &middot; ${esc(trends.timeframe||"")}</div>
          <div class="ab-text">${trends.average_interest ?? 0} / 100 average &mdash; ${esc(m.trending_interest||"Unknown")} relative interest</div>
          ${sparkline}
        </div>
        ${relatedChips}
      </div>` : `
      <div class="ai-board" style="margin-bottom:22px;">
        <div class="ab-head">Google Trends</div>
        <div class="ab-text" style="color:var(--ink-faint);font-style:italic;">No Google Trends data for this run — check the errors panel below if this is unexpected.</div>
      </div>`;

    box.innerHTML = cachedBanner + header + metricsPanel + trendsSection + errBlock + aiBoard + feedSection;

    box.querySelectorAll(".fg-btn").forEach(b=>b.addEventListener("click", ()=>setFeedGroupMode(b.dataset.mode)));
    renderFeedBody();
  }

  // ---------- Aggregated Feed: 3 grouping modes ----------
  // "newest"  — every persona's posts merged into one date-sorted coverflow (original behavior).
  // "persona" — a labeled section per persona (Official Page, CEO, CFO, ...), stacked posts underneath.
  // "tier"    — two labeled sections (Tier 1 official sources / Tier 2 named executives), each broken
  //             down by persona underneath — same grouping, organized by hierarchy first.

  function setFeedGroupMode(mode){
    window.__feedGroupMode = mode;
    document.querySelectorAll(".fg-btn").forEach(b=>b.classList.toggle("active", b.dataset.mode===mode));
    renderFeedBody();
  }

  function feedPostCardHTML(it){
    const initials = (it.author||"?").split(" ").map(w=>w[0]).slice(0,2).join("").toUpperCase();
    const avatar = it.author_photo
      ? `<img class="avatar" src="${esc(it.author_photo)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar',textContent:'${esc(initials)}'}))">`
      : `<div class="avatar">${esc(initials)}</div>`;
    return `<div class="post-card" style="margin-bottom:12px;">
      <div class="pc-person">
        ${avatar}
        <div class="pc-person-info">
          <div class="pc-name">${esc(it.author||it.persona||"Unknown")}</div>
          <div class="pc-source">${esc(sourceLabel(it.source))} · ${esc(it.date||"—")}${it.unverified?" · unverified match":""}</div>
        </div>
      </div>
      <div class="ptext">${esc(it.text)||"<span style='color:var(--ink-faint)'>(no text)</span>"}</div>
      <div class="post-foot">
        <span>${it.reactions||0} likes</span><span>${it.comments||0} comments</span>
        ${it.url?`<a class="open-btn ${it.source==='x'?'x':'li'}" href="${esc(it.url)}" target="_blank">open ↗</a>`:""}
      </div>
    </div>`;
  }

  function personaSectionHTML(persona, tier, items){
    if(!items.length) return "";
    return `<div class="feed-persona">
      <div class="feed-persona-head">
        <span class="fp-tier ${tier===1?'t1':'t2'}">${tier===1?'Tier 1':'Tier 2'}</span>
        <span class="fp-name">${esc(persona)}</span>
        <span class="fp-count">${items.length} post${items.length===1?"":"s"}</span>
      </div>
      ${items.map(feedPostCardHTML).join("")}
    </div>`;
  }

  function feedGroupsFromData(data){
    // Backend already groups+sorts by tier then persona; just drop website "posts".
    return (data.feed || [])
      .map(g=>({persona: g.persona, tier: g.tier, items: (g.items||[]).filter(it=>it.source!=="website")}))
      .filter(g=>g.items.length);
  }

  function renderFeedBody(){
    const mode = window.__feedGroupMode || "newest";
    const data = window.__intelFeedData;
    const merged = window.__intelFeedMerged || [];
    const bodyEl = $("#feedBody");
    if(!bodyEl || !data) return;

    if(mode === "newest"){
      const showArrows = merged.length > 1;
      bodyEl.innerHTML = merged.length ? `
        <div class="coverflow-wrap">
          ${showArrows?`<button class="arrow icf-prev">‹</button>`:""}
          <div class="coverflow-stage compact" id="icfStage"></div>
          ${showArrows?`<button class="arrow icf-next">›</button>`:""}
        </div>
        ${showArrows?`<div class="viewer-dots" id="icfDots"></div>`:""}
      ` : '<div class="empty">No posts matched the time filter.</div>';
      window.__intelSlider = { items: merged, idx: 0 };
      if(merged.length){
        renderIntelSliderStage();
        bodyEl.querySelectorAll(".icf-prev").forEach(b=>b.addEventListener("click", ()=>moveIntelSlider(-1)));
        bodyEl.querySelectorAll(".icf-next").forEach(b=>b.addEventListener("click", ()=>moveIntelSlider(1)));
      }
      return;
    }

    const groups = feedGroupsFromData(data);
    if(!groups.length){ bodyEl.innerHTML = '<div class="empty">No posts matched the time filter.</div>'; return; }

    if(mode === "persona"){
      bodyEl.innerHTML = groups.map(g=>personaSectionHTML(g.persona, g.tier, g.items)).join("");
      return;
    }

    // mode === "tier"
    const t1 = groups.filter(g=>g.tier===1), t2 = groups.filter(g=>g.tier===2);
    const tierBlock = (title, arr) => arr.length ? `<div style="margin-bottom:22px;">
        <div style="font-size:13px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;color:var(--ink-faint);margin:0 0 12px 2px;padding-bottom:7px;border-bottom:1px dashed var(--line);">${esc(title)}</div>
        ${arr.map(g=>personaSectionHTML(g.persona, g.tier, g.items)).join("")}
      </div>` : "";
    bodyEl.innerHTML = tierBlock("Tier 1 — Official Sources", t1) + tierBlock("Tier 2 — Named Executives", t2);
  }

  function renderIntelSliderStage(){
    const state = window.__intelSlider;
    const stage = document.getElementById("icfStage");
    if(!state || !stage) return;
    const { items, idx } = state;
    stage.innerHTML = items.map((it, i)=>{
      const offset = i - idx;
      if(Math.abs(offset) > 2) return "";
      const initials = (it.author||"?").split(" ").map(w=>w[0]).slice(0,2).join("").toUpperCase();
      const avatar = it.author_photo
        ? `<img class="avatar" src="${esc(it.author_photo)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar',textContent:'${esc(initials)}'}))">`
        : `<div class="avatar">${esc(initials)}</div>`;
      const tierTag = `<span class="fp-tier ${it.tier===1?'t1':'t2'}" style="margin-left:auto;flex:0 0 auto;">${it.tier===1?'Tier 1':'Tier 2'}</span>`;
      return `<div class="cf-card" data-i="${i}" data-current="${i===idx?1:0}" style="${cfTransform(offset)}">
        <div class="post-card">
          <div class="pc-person">
            ${avatar}
            <div class="pc-person-info">
              <div class="pc-name">${esc(it.author||it.persona||"Unknown")}</div>
              <div class="pc-source">${esc(it.persona!==it.author?it.persona:"")}${it.persona!==it.author?" · ":""}${esc(sourceLabel(it.source))} · ${esc(it.date||"—")}${it.unverified?" · unverified match":""}</div>
            </div>
            ${tierTag}
          </div>
          <div class="ptext">${esc(it.text)||"<span style='color:var(--ink-faint)'>(no text)</span>"}</div>
          <div class="post-foot">
            <span>${it.reactions||0} likes</span><span>${it.comments||0} comments</span>
            ${it.url?`<a class="open-btn ${it.source==='x'?'x':'li'}" href="${esc(it.url)}" target="_blank">open ↗</a>`:""}
          </div>
        </div></div>`;
    }).join("");
    stage.querySelectorAll(".cf-card").forEach(el=>el.addEventListener("click",(e)=>{
      if(e.target.closest(".open-btn")) return;
      const i=parseInt(el.dataset.i);
      if(i!==state.idx){ state.idx=i; renderIntelSliderStage(); }
    }));
    updateIntelSliderControls();
  }

  function moveIntelSlider(delta){
    const state = window.__intelSlider;
    if(!state) return;
    state.idx = Math.max(0, Math.min(state.items.length-1, state.idx+delta));
    renderIntelSliderStage();
  }

  function updateIntelSliderControls(){
    const state = window.__intelSlider;
    const prev = document.querySelector(".icf-prev"), next = document.querySelector(".icf-next");
    if(prev) prev.disabled = state.idx<=0;
    if(next) next.disabled = state.idx>=state.items.length-1;
    const dots = document.getElementById("icfDots");
    if(dots){
      dots.innerHTML = state.items.map((_,i)=>`<span class="dot ${i===state.idx?'active':''}" data-i="${i}"></span>`).join("");
      dots.querySelectorAll(".dot").forEach(d=>d.addEventListener("click",()=>{
        state.idx = parseInt(d.dataset.i); renderIntelSliderStage();
      }));
    }
  }

  // Keyboard ← → also drives the merged Company Intelligence slider while it's on screen.
  document.addEventListener("keydown", e=>{
    if(currentView!=="intelView") return;
    if(!window.__intelSlider || !window.__intelSlider.items.length) return;
    if(document.activeElement && ["INPUT","TEXTAREA"].includes(document.activeElement.tagName)) return;
    if(e.key==="ArrowLeft"){ e.preventDefault(); moveIntelSlider(-1); }
    else if(e.key==="ArrowRight"){ e.preventDefault(); moveIntelSlider(1); }
  });

  function exportIntelligence(fmt){
    if(!intelLastResult) return;
    const data = intelLastResult;
    const safe = (data.company_name||"company").replace(/[^a-z0-9]+/gi,"_").replace(/^_+|_+$/g,"") || "company";
    const stamp = new Date().toISOString().slice(0,10);
    let blob, filename;
    if(fmt === "json"){
      blob = new Blob([JSON.stringify(data, null, 2)], {type:"application/json"});
      filename = `signal_intel_${safe}_${stamp}.json`;
    }else{
      const cols = ["tier","persona","source","author","date","text","reactions","comments","url","unverified"];
      const rows = [cols.join(",")];
      (data.items_flat||[]).forEach(it=>{
        rows.push([
          csvCell(it.tier), csvCell(it.persona), csvCell(it.source), csvCell(it.author),
          csvCell(it.date), csvCell(it.text), csvCell(it.reactions||0), csvCell(it.comments||0),
          csvCell(it.url), csvCell(!!it.unverified),
        ].join(","));
      });
      blob = new Blob(["\ufeff"+rows.join("\r\n")], {type:"text/csv;charset=utf-8;"});
      filename = `signal_intel_${safe}_${stamp}.csv`;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  }

  // Pre-warm the "Previously Analyzed" grid (lives in Company Profiles) so it's
  // ready instantly the first time the user navigates there.
  loadCachedIntelligence();

  // ---------- Data Sources (config-driven, pluggable) ----------
  let srcDraftConfig = null;
  let srcLastTestError = "";
  let srcLastZeroItems = false;
  let srcDebugHistory = [];  // [{error, zero_items, ai_diagnosis, applied_fix, fixed_config}] this draft session

  async function openSources(){
    navigateTo("sourcesView");
    $("#srcDraft").innerHTML = "";
    loadSourcesList();
  }

  function srcSetStatus(show, text){
    const s = $("#srcStatus");
    if(show){ s.classList.remove("hidden"); $("#srcStatusText").textContent = text||"Working…"; }
    else s.classList.add("hidden");
  }

  async function extractSource(){
    const docs = $("#srcDocs").value.trim();
    if(!docs){ $("#srcDraft").innerHTML = '<div class="error">Paste some API documentation first.</div>'; return; }
    const btn = $("#srcExtractBtn"); btn.disabled = true;
    srcSetStatus(true, "Asking Gemini to extract a config…");
    $("#srcDraft").innerHTML = "";
    srcLastTestError = ""; srcLastZeroItems = false;
    try{
      const r = await fetch("/api/sources/extract", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({docs, hints: $("#srcHints").value.trim(), api_key: $("#srcApiKey").value.trim()})});
      if(!r.ok){ const e = await r.json(); throw new Error(e.detail || r.statusText); }
      const data = await r.json();
      srcDraftConfig = data.config;
      renderSourceDraft(data.config, data.problems || []);
    }catch(err){
      $("#srcDraft").innerHTML = '<div class="error">Extraction failed: '+esc(err.message)+'</div>';
    }finally{
      btn.disabled = false; srcSetStatus(false);
    }
  }

  function renderSourceDraft(cfg, problems){
    const box = $("#srcDraft");
    const pretty = esc(JSON.stringify(cfg, null, 2));
    const probHtml = (problems && problems.length)
      ? `<div class="error" style="text-align:left;padding:10px 14px;margin-bottom:10px;">Needs fixing before saving:<br>${problems.map(p=>"• "+esc(p)).join("<br>")}</div>`
      : `<div class="hint" style="margin-bottom:10px;">Looks valid. Test it against a sample company, then save.</div>`;
    box.innerHTML = `
      <div class="ai-board" style="margin-bottom:22px;">
        <div class="ab-head">Draft config${cfg.display_name?` — ${esc(cfg.display_name)}`:""}</div>
        ${probHtml}
        <textarea id="srcDraftJson" rows="16" style="width:100%;background:var(--bg);border:1px solid var(--line);color:var(--ink);padding:11px 13px;border-radius:8px;font-size:12px;font-family:'SF Mono','Consolas',monospace;resize:vertical;">${pretty}</textarea>
        <div class="row" style="margin-top:12px;">
          <div class="field" style="flex:0 0 200px;"><label for="srcTestTarget">Test with company</label>
            <input type="text" id="srcTestTarget" value="Microsoft"></div>
          <button class="go batch-btn secondary" onclick="testSource()">Test</button>
          <button class="go batch-btn secondary" style="border-color:var(--intel);color:var(--intel);" onclick="debugSource()">Debug</button>
          <button class="go intel-btn" onclick="saveSource()">Save source</button>
        </div>
        <div id="srcTestResult"></div>
        <div id="srcDebugResult"></div>
      </div>`;
    srcLastTestError = ""; srcLastZeroItems = false; srcDebugHistory = [];
  }

  function readDraftJson(){
    try{ return JSON.parse($("#srcDraftJson").value); }
    catch(e){ $("#srcTestResult").innerHTML = '<div class="error">Config isn\'t valid JSON: '+esc(e.message)+'</div>'; return null; }
  }

  async function testSource(){
    const cfg = readDraftJson(); if(!cfg) return;
    const target = $("#srcTestTarget").value.trim() || "Microsoft";
    const box = $("#srcTestResult");
    $("#srcDebugResult").innerHTML = "";
    box.innerHTML = '<div class="loading">Testing…</div>';
    try{
      const r = await fetch("/api/sources/test", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({config: cfg, target, max_items: 3})});
      const data = await r.json();
      if(!r.ok){ throw new Error(data.detail || r.statusText); }
      if(!data.ok){
        srcLastTestError = data.error || "unknown error"; srcLastZeroItems = false;
        box.innerHTML = '<div class="error">Test failed: '+esc(srcLastTestError)+'</div><div class="hint" style="margin-top:8px;">Hit Debug for a diagnosis.</div>';
        return;
      }
      if(!data.samples.length){
        srcLastTestError = ""; srcLastZeroItems = true;
        box.innerHTML = '<div class="empty">Call succeeded but returned 0 items — check response_path / target_type. Hit Debug for help.</div>';
        return;
      }
      srcLastTestError = ""; srcLastZeroItems = false;
      box.innerHTML = `<div class="hint" style="margin:12px 0 8px;">Got ${data.raw_count} item(s). Sample:</div>` +
        data.samples.map(s=>`<div class="post-card" style="margin-bottom:10px;">
          <div class="ptext" style="max-height:120px;">${esc(s.text)||"<span style='color:var(--ink-faint)'>(no text)</span>"}</div>
          <div class="post-foot"><span>${esc(s.date||"—")}</span><span>${s.reactions||0} reactions</span><span>${s.comments||0} comments</span>
          ${s.url?`<a class="open-btn li" href="${esc(s.url)}" target="_blank">open ↗</a>`:""}</div>
        </div>`).join("");
    }catch(err){
      srcLastTestError = err.message; srcLastZeroItems = false;
      box.innerHTML = '<div class="error">Test failed: '+esc(err.message)+'</div><div class="hint" style="margin-top:8px;">Hit Debug for a diagnosis.</div>';
    }
  }

  async function debugSource(){
    const cfg = readDraftJson(); if(!cfg) return;
    const target = $("#srcTestTarget") ? ($("#srcTestTarget").value.trim() || "Microsoft") : "Microsoft";
    const box = $("#srcDebugResult");
    const attemptNum = srcDebugHistory.length + 1;
    box.innerHTML = '<div class="loading">Diagnosing'+(attemptNum>1?` (attempt #${attemptNum})`:'')+'…'+(srcLastZeroItems?' fetching the real response to check the shape…':'')+'</div>';
    try{
      const r = await fetch("/api/sources/debug", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({config: cfg, error: srcLastTestError, docs: $("#srcDocs").value, target, max_items: 3,
                               zero_items: srcLastZeroItems, history: srcDebugHistory})});
      const data = await r.json();
      if(!r.ok){ throw new Error(data.detail || r.statusText); }

      const repeatBanner = attemptNum > 1
        ? `<div class="hint" style="color:var(--intel);margin-bottom:10px;">This is attempt #${attemptNum} on the same draft — Gemini was shown ${srcDebugHistory.length} prior attempt(s) and told not to repeat a diagnosis that already didn't work.</div>`
        : "";
      const schemaHtml = (data.schema_problems && data.schema_problems.length)
        ? `<div class="ai-block"><div class="ab-label">Schema problems</div><div class="ab-text">${data.schema_problems.map(p=>"• "+esc(p)).join("<br>")}</div></div>` : "";
      const ruleHtml = (data.rule_findings && data.rule_findings.length)
        ? `<div class="ai-block"><div class="ab-label">Automated checks</div><div class="ab-text">${data.rule_findings.map(p=>"• "+esc(p)).join("<br>")}</div></div>` : "";
      const aiHtml = data.ai_diagnosis
        ? `<div class="ai-block"><div class="ab-label">Gemini diagnosis</div><div class="ab-text">${esc(data.ai_diagnosis)}</div></div>` : "";
      const rawHtml = data.raw_response_sample
        ? `<div class="ai-block"><div class="ab-label">Real raw API response (truncated)</div>
             <textarea readonly rows="12" style="width:100%;background:var(--bg);border:1px solid var(--line);color:var(--ink-dim);padding:10px 12px;border-radius:8px;font-size:11px;font-family:'SF Mono','Consolas',monospace;">${esc(data.raw_response_sample)}</textarea>
           </div>` : "";
      const rawErrHtml = data.raw_fetch_error
        ? `<div class="ai-block"><div class="ab-label">Couldn't fetch a sample response</div><div class="ab-text">${esc(data.raw_fetch_error)}</div></div>` : "";
      const fixBtn = data.fixed_config
        ? `<button class="go intel-btn" style="margin-top:8px;" onclick='applyFixedSourceConfig(${JSON.stringify(JSON.stringify(data.fixed_config))})'>Apply suggested fix</button>`
        : "";
      const historyToggle = srcDebugHistory.length
        ? `<details style="margin-top:12px;"><summary style="cursor:pointer;font-size:12px;color:var(--ink-faint);">Previous attempt(s) on this draft (${srcDebugHistory.length})</summary>
             ${srcDebugHistory.map((h,i)=>`<div class="ai-block" style="margin-top:8px;">
               <div class="ab-label">Attempt ${i+1} — ${h.zero_items?'0 items returned':(h.error?esc(h.error):'no error recorded')}</div>
               <div class="ab-text" style="color:var(--ink-dim);">${esc(h.ai_diagnosis || '(no diagnosis returned)')}${h.applied_fix?' — fix was applied afterward.':''}</div>
             </div>`).join("")}
           </details>`
        : "";

      box.innerHTML = `<div class="ai-board" style="margin-top:14px;border-color:var(--line);">
        <div class="ab-head">Debug results${attemptNum>1?` — attempt #${attemptNum}`:''}</div>
        ${repeatBanner}
        ${schemaHtml}${ruleHtml}${aiHtml}${rawErrHtml}
        ${!schemaHtml && !ruleHtml && !aiHtml ? '<div class="ab-text" style="color:var(--ink-faint);font-style:italic;">Nothing obviously wrong found. Double-check the API key is actually valid and subscribed/enabled on the provider’s side.</div>' : ""}
        ${fixBtn}
        ${rawHtml}
        ${historyToggle}
      </div>`;

      // Record this attempt so a repeat Debug on the same unresolved symptom
      // gets escalated guidance instead of the identical diagnosis again.
      srcDebugHistory.push({
        error: srcLastTestError,
        zero_items: srcLastZeroItems,
        ai_diagnosis: data.ai_diagnosis || "",
        fixed_config: data.fixed_config || null,
        applied_fix: false,
      });
    }catch(err){
      box.innerHTML = '<div class="error">Debug failed: '+esc(err.message)+'</div>';
    }
  }

  function applyFixedSourceConfig(jsonStr){
    try{
      const fixed = JSON.parse(jsonStr);
      $("#srcDraftJson").value = JSON.stringify(fixed, null, 2);
      if(srcDebugHistory.length) srcDebugHistory[srcDebugHistory.length-1].applied_fix = true;
      $("#srcDebugResult").innerHTML = '<div class="hint" style="margin-top:10px;">Applied — hit Test again to confirm.</div>';
    }catch(e){ /* no-op, malformed */ }
  }

  async function saveSource(){
    const cfg = readDraftJson(); if(!cfg) return;
    try{
      const r = await fetch("/api/sources", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({config: cfg})});
      const data = await r.json();
      if(!r.ok){ throw new Error(data.detail || r.statusText); }
      $("#srcDraft").innerHTML = '<div class="hint" style="color:var(--intel);">Saved “'+esc(cfg.name)+'”. It now runs on every Company Intelligence search.</div>';
      $("#srcDocs").value = ""; $("#srcHints").value = ""; $("#srcApiKey").value = "";
      srcDraftConfig = null;
      loadSourcesList();
    }catch(err){ $("#srcTestResult").innerHTML = '<div class="error">Save failed: '+esc(err.message)+'</div>'; }
  }

  async function loadSourcesList(){
    const box = $("#srcList");
    box.innerHTML = '<div class="loading">Loading sources…</div>';
    try{
      const data = await (await fetch("/api/sources")).json();
      if(!data.sources || !data.sources.length){ box.innerHTML = '<div class="empty">No custom sources yet. Built-in sources (LinkedIn, X, Website, Google Trends) always run; add more above.</div>'; return; }
      box.innerHTML = `<div class="results-head"><h2>Custom sources</h2><span class="meta">${data.sources.length} configured</span></div>` +
        data.sources.map(c=>{
          const authType = (c.auth&&c.auth.type)||"none";
          const keyNote = (c.auth&&c.auth._key_set)?` · key ${esc(c.auth.key_value)}`:"";
          return `<div class="person" style="cursor:default;">
            <div class="person-main" style="cursor:default;">
              <div class="avatar">${esc((c.display_name||c.name||"?")[0].toUpperCase())}</div>
              <div class="person-id">
                <div class="nm">${esc(c.display_name||c.name)} <span class="tier-badge ${c.tier===2?'tier-vp':'tier-c-suite'}">Tier ${c.tier||1}</span> ${c.enabled?'<span class="tier-badge tier-c-suite">ON</span>':'<span class="tier-badge">OFF</span>'}</div>
                <div class="hl">${esc(c.method||"GET")} ${esc(c.base_url||"")}${esc(c.list_endpoint||"")}</div>
                <div class="loc">auth: ${esc(authType)}${keyNote} · target: ${esc(c.target_type||"name")}</div>
              </div>
              <button class="vt" onclick="toggleSource('${esc(c.name)}')">${c.enabled?'Disable':'Enable'}</button>
              <button class="vt" style="margin-left:6px;color:var(--err);" onclick="deleteSource('${esc(c.name)}')">Delete</button>
            </div>
          </div>`;
        }).join("");
    }catch(err){ box.innerHTML = '<div class="error">Could not load sources.</div>'; }
  }

  async function toggleSource(name){
    try{ await fetch(`/api/sources/${encodeURIComponent(name)}/toggle`, {method:"POST"}); loadSourcesList(); }catch{}
  }
  async function deleteSource(name){
    if(!confirm(`Delete source “${name}”? This removes its config file.`)) return;
    try{ await fetch(`/api/sources/${encodeURIComponent(name)}`, {method:"DELETE"}); loadSourcesList(); }catch{}
  }

  // ---------- history ----------
  async function loadHistory(){
    if(state.platform!=="x") return;
    try{ const data=await (await fetch("/api/history")).json(); renderHistory(data.entries||[]); }catch{}
  }
  function renderHistory(entries){
    const rel = entries.filter(e=>e.kind==="x");
    const panel = $("#historyPanelX"), list = $("#historyListX");
    if(!panel||!list) return;
    if(!rel.length){ panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    list.innerHTML=rel.map(e=>`<div class="hist-item" data-kind="${esc(e.kind)}" data-label="${esc(e.label)}">
      <span class="hist-kind ${esc(e.kind)}">${esc(e.kind)}</span>
      <span class="hist-label">${esc(e.label)}</span>
      <span class="hist-meta">${e.count!=null?e.count+" results":""}</span></div>`).join("");
    list.querySelectorAll(".hist-item").forEach(it=>it.addEventListener("click",()=>{
      xTweets(it.dataset.label.replace(/^@/,""));
    }));
  }
  async function clearHistory(){ try{ await fetch("/api/history",{method:"DELETE"}); loadHistory(); }catch{} }
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)