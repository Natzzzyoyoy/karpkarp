from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import time
from collections import defaultdict
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse

import fitz
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONTACT_EMAIL = os.getenv("KARP_CONTACT_EMAIL", "")
BRAVE_API_KEY = os.getenv("KARP_BRAVE_API_KEY", "")
OPENALEX_API_KEY = os.getenv("KARP_OPENALEX_API_KEY", "")
MAX_TEXT_CHARS = 60_000
WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)

app = FastAPI(
    title="Karp Plagiarism Checker API",
    description="Early-stage phrase matching against public web and scholarly metadata.",
    version="1.0.0",
)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


class CheckRequest(BaseModel):
    text: str = Field(min_length=15, max_length=MAX_TEXT_CHARS)
    scholar: bool = True
    web: bool = True
    deep: bool = True


def token_spans(text: str) -> list[dict]:
    return [
        {"word": m.group(0), "norm": m.group(0).casefold().replace("’", "'"), "start": m.start(), "end": m.end()}
        for m in WORD_RE.finditer(text)
    ]


def word_count(text: str) -> int:
    return len(token_spans(text))


def ngrams_from_tokens(tokens: list[dict], n: int = 5) -> set[str]:
    if len(tokens) < n:
        return {" ".join(t["norm"] for t in tokens)} if tokens else set()
    return {" ".join(tokens[i + j]["norm"] for j in range(n)) for i in range(len(tokens) - n + 1)}


def build_queries(text: str, count: int = 2) -> list[str]:
    """Choose a couple of reasonably specific text excerpts to search for."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    parts = [p for p in parts if word_count(p) >= 8]
    if not parts:
        parts = [text.strip()]
    # Prefer substantive sentences but retain order for reproducible results.
    ranked = sorted(parts, key=lambda p: min(word_count(p), 35), reverse=True)
    chosen: list[str] = []
    for p in ranked:
        toks = token_spans(p)
        if len(toks) > 12:
            # Keep a text fragment rather than a very long exact-phrase query.
            start = max(0, (len(toks) - 10) // 3)
            p = p[toks[start]["start"]:toks[start + 10]["end"]]
        if p.casefold() not in {x.casefold() for x in chosen}:
            chosen.append(p)
        if len(chosen) >= count:
            break
    if not chosen:
        toks = token_spans(text)
        if len(toks) >= 8:
            chosen = [text[toks[0]["start"]:toks[min(9, len(toks) - 1)]["end"]]]
    return chosen[:count]


def safe_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.rstrip(".").casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False
        except ValueError:
            # Search results are external URLs; reject local-looking hostnames.
            if "." not in host:
                return False
        return True
    except Exception:
        return False


def reconstruct_abstract(inverted_index) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    words: dict[int, str] = {}
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and 0 <= position < 100_000:
                words[position] = word
    if not words:
        return ""
    return " ".join(words[i] for i in range(max(words) + 1) if i in words)


def clean_text_from_html(raw_html: bytes) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    pieces = [el.get_text(" ", strip=True) for el in root.find_all(["p", "li", "blockquote", "h1", "h2", "h3"]) if el.get_text(strip=True)]
    text = "\n".join(pieces) if pieces else root.get_text(" ", strip=True)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:100_000]


async def fetch_json(client: httpx.AsyncClient, url: str, params: dict) -> dict | None:
    try:
        response = await client.get(url, params=params, timeout=8.0)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def openalex_records(data: dict | None) -> list[dict]:
    records = []
    for item in (data or {}).get("results", []) or []:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        authors = []
        for au in item.get("authorships", []) or []:
            name = (au.get("author") or {}).get("display_name")
            if name:
                authors.append(name)
        primary = item.get("primary_location") or {}
        venue = ((primary.get("source") or {}).get("display_name") or "").strip()
        landing = primary.get("landing_page_url") or ""
        doi = item.get("doi") or ""
        url = doi if doi.startswith("https://") else (landing or item.get("id") or "")
        best_oa = item.get("best_oa_location") or {}
        abstract = reconstruct_abstract(item.get("abstract_inverted_index"))
        records.append({
            "title": title,
            "url": url,
            "type": "preprint" if "arxiv" in (url or "").casefold() else "jurnal",
            "authors": ", ".join(authors[:4]),
            "venue": venue,
            "year": item.get("publication_year") or "",
            "snippet": abstract[:3500],
            "text": " ".join([title, abstract]),
            "doi": doi,
            "pdf_url": best_oa.get("pdf_url") or "",
            "full_text_checked": False,
        })
    return records


def crossref_records(data: dict | None) -> list[dict]:
    records = []
    for item in (((data or {}).get("message") or {}).get("items") or []):
        title = ((item.get("title") or [""])[0] or "").strip()
        if not title:
            continue
        doi = item.get("DOI") or ""
        url = f"https://doi.org/{doi}" if doi else (((item.get("link") or [{}])[0]).get("URL") or "")
        authors = []
        for au in item.get("author", []) or []:
            name = " ".join(x for x in [au.get("given"), au.get("family")] if x)
            if name:
                authors.append(name)
        container = item.get("container-title") or []
        date_parts = (((item.get("published") or {}).get("date-parts") or [[None]])[0] or [None])
        year = date_parts[0] if date_parts else ""
        abstract = re.sub(r"<[^>]+>", " ", item.get("abstract") or "")
        abstract = re.sub(r"\s+", " ", abstract).strip()
        records.append({
            "title": title,
            "url": url,
            "type": "jurnal",
            "authors": ", ".join(authors[:4]),
            "venue": (container[0] if container else ""),
            "year": year or "",
            "snippet": abstract[:3500],
            "text": " ".join([title, abstract]),
            "doi": doi,
            "pdf_url": "",
            "full_text_checked": False,
        })
    return records


async def search_openalex(client: httpx.AsyncClient, query: str) -> list[dict]:
    params = {
        "search": query,
        "per-page": 5,
        "select": "id,title,doi,publication_year,authorships,primary_location,abstract_inverted_index,best_oa_location",
    }
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    data = await fetch_json(client, "https://api.openalex.org/works", params)
    return openalex_records(data)


async def search_crossref(client: httpx.AsyncClient, query: str) -> list[dict]:
    params = {"query.bibliographic": query, "rows": 5, "select": "DOI,title,author,container-title,published,URL,link,abstract"}
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    data = await fetch_json(client, "https://api.crossref.org/works", params)
    return crossref_records(data)


async def search_duckduckgo(client: httpx.AsyncClient, query: str) -> list[dict]:
    try:
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f'"{query}"'},
            headers={"User-Agent": "Mozilla/5.0 (compatible; KarpChecker/1.0)"},
            timeout=8.0,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for result in soup.select(".result")[:8]:
            link = result.select_one("a.result__a")
            if not link:
                continue
            url = link.get("href", "")
            if "duckduckgo.com/l/?" in url:
                values = parse_qs(urlparse(url).query)
                url = (values.get("uddg") or [url])[0]
            if not safe_public_url(url):
                continue
            snippet_el = result.select_one(".result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            records.append({
                "title": link.get_text(" ", strip=True),
                "url": url,
                "type": "web",
                "authors": "",
                "venue": urlparse(url).netloc,
                "year": "",
                "snippet": snippet[:1200],
                "text": " ".join([link.get_text(" ", strip=True), snippet]),
                "doi": "",
                "pdf_url": "",
                "full_text_checked": False,
            })
        return records
    except Exception:
        return []


async def search_brave(client: httpx.AsyncClient, query: str) -> list[dict]:
    if not BRAVE_API_KEY:
        return []
    try:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": f'"{query}"', "count": 8},
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
            timeout=8.0,
        )
        response.raise_for_status()
        data = response.json()
        records = []
        for item in ((data.get("web") or {}).get("results") or []):
            url = item.get("url", "")
            if not safe_public_url(url):
                continue
            title = item.get("title", "")
            snippet = item.get("description", "")
            records.append({
                "title": title,
                "url": url,
                "type": "web",
                "authors": "",
                "venue": urlparse(url).netloc,
                "year": "",
                "snippet": snippet[:1200],
                "text": " ".join([title, snippet]),
                "doi": "",
                "pdf_url": "",
                "full_text_checked": False,
            })
        return records
    except Exception:
        return []


async def read_public_source(client: httpx.AsyncClient, url: str, pdf_url: str = "") -> str:
    """Read a bounded amount of accessible public text; never follows redirects."""
    candidates = [pdf_url, url] if pdf_url else [url]
    for candidate in candidates:
        if not candidate or not safe_public_url(candidate):
            continue
        try:
            async with client.stream(
                "GET", candidate,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KarpChecker/1.0)", "Range": "bytes=0-2500000"},
                timeout=7.0,
            ) as response:
                if response.status_code not in (200, 206):
                    continue
                ctype = response.headers.get("content-type", "").casefold()
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    remaining = 2_500_000 - size
                    if remaining <= 0:
                        break
                    chunk = chunk[:remaining]
                    chunks.append(chunk)
                    size += len(chunk)
                raw = b"".join(chunks)
                if not raw:
                    continue
                if "pdf" in ctype or candidate.casefold().split("?", 1)[0].endswith(".pdf") or raw.startswith(b"%PDF"):
                    try:
                        doc = fitz.open(stream=raw, filetype="pdf")
                        chunks_text = []
                        for page in doc[:8]:
                            chunks_text.append(page.get_text("text"))
                        doc.close()
                        text = re.sub(r"\s+", " ", " ".join(chunks_text)).strip()
                    except Exception:
                        text = ""
                elif "html" in ctype or raw.lstrip().startswith(b"<"):
                    text = clean_text_from_html(raw)
                else:
                    text = raw.decode("utf-8", errors="ignore")
                if len(text) >= 150:
                    return text[:100_000]
        except Exception:
            continue
    return ""


def calculate_match(text: str, candidate_text: str) -> tuple[float, set[str], list[tuple[int, int]]]:
    suspect_tokens = token_spans(text)
    candidate_tokens = token_spans(candidate_text)
    if not suspect_tokens or not candidate_tokens:
        return 0.0, set(), []
    suspect_grams = ngrams_from_tokens(suspect_tokens)
    candidate_grams = ngrams_from_tokens(candidate_tokens)
    common = suspect_grams & candidate_grams
    score = round(len(common) / max(len(suspect_grams), 1) * 100)
    n = 5 if len(suspect_tokens) >= 5 else len(suspect_tokens)
    matches: list[tuple[int, int]] = []
    if n:
        for i in range(len(suspect_tokens) - n + 1):
            gram = " ".join(suspect_tokens[i + j]["norm"] for j in range(n))
            if gram in candidate_grams:
                matches.append((i, i + n - 1))
    return score, common, matches


def phrases_for(text: str, matched_positions: list[tuple[int, int]], max_phrases: int = 6) -> list[str]:
    tokens = token_spans(text)
    if not tokens or not matched_positions:
        return []
    ranges = sorted(set(matched_positions))
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    phrases = []
    for start, end in merged:
        phrase = text[tokens[start]["start"]:tokens[end]["end"]].strip()
        if phrase and phrase.casefold() not in {p.casefold() for p in phrases}:
            phrases.append(phrase)
        if len(phrases) >= max_phrases:
            break
    return phrases


def build_segments(text: str, mark_sources: dict[int, set[int]]) -> list[dict]:
    tokens = token_spans(text)
    if not tokens:
        return [{"text": text, "sources": []}]
    segments: list[dict] = []
    cursor = 0
    for index, token in enumerate(tokens):
        if cursor < token["start"]:
            gap = text[cursor:token["start"]]
            if segments and segments[-1]["sources"] == []:
                segments[-1]["text"] += gap
            else:
                segments.append({"text": gap, "sources": []})
        source_ids = sorted(mark_sources.get(index, set()))
        part_end = tokens[index + 1]["start"] if index + 1 < len(tokens) else len(text)
        part = text[token["start"]:part_end]
        if segments and segments[-1]["sources"] == source_ids:
            segments[-1]["text"] += part
        else:
            segments.append({"text": part, "sources": source_ids})
        cursor = part_end
    if cursor < len(text):
        tail = text[cursor:]
        if segments and segments[-1]["sources"] == []:
            segments[-1]["text"] += tail
        else:
            segments.append({"text": tail, "sources": []})
    return segments


@app.get("/")
def homepage():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "karp-checker", "version": "1.0.0"}


@app.post("/api/check")
async def check_text(request: CheckRequest):
    started = time.perf_counter()
    text = request.text.strip()
    if word_count(text) < 15:
        raise HTTPException(status_code=400, detail="Teks minimal 15 kata ya, supaya pencarian lebih bermakna.")
    if not request.scholar and not request.web:
        raise HTTPException(status_code=400, detail="Pilih minimal satu sumber: jurnal ilmiah atau web umum.")

    queries = build_queries(text, count=2)
    if not queries:
        raise HTTPException(status_code=400, detail="Belum cukup teks untuk dijadikan kueri pencarian.")

    warnings: list[str] = []
    headers = {"User-Agent": "KarpChecker/1.0 (academic phrase matching)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=False) as client:
        jobs = []
        labels = []
        for query in queries:
            if request.scholar:
                jobs.extend([search_openalex(client, query), search_crossref(client, query)])
                labels.extend(["openalex", "crossref"])
            if request.web:
                jobs.append(search_brave(client, query) if BRAVE_API_KEY else search_duckduckgo(client, query))
                labels.append("web")
        results = await asyncio.gather(*jobs, return_exceptions=True)

        candidates: list[dict] = []
        for label, result in zip(labels, results):
            if isinstance(result, Exception) or not isinstance(result, list):
                if label == "web":
                    warnings.append("Pencarian web umum tidak merespons; hasil jurnal masih dapat tersedia.")
                else:
                    warnings.append(f"Layanan metadata {label.capitalize()} sementara tidak merespons.")
                continue
            candidates.extend(result)

        # Deduplicate DOI first, then canonical URL, then title.
        unique: dict[str, dict] = {}
        for item in candidates:
            doi = (item.get("doi") or "").casefold().replace("https://doi.org/", "")
            url = (item.get("url") or "").split("#", 1)[0].rstrip("/").casefold()
            title_key = re.sub(r"\W+", " ", (item.get("title") or "").casefold()).strip()
            key = f"doi:{doi}" if doi else (f"url:{url}" if url else f"title:{title_key}")
            if key == "doi:":
                key = f"title:{title_key}"
            if key in unique:
                old = unique[key]
                if len(item.get("text", "")) > len(old.get("text", "")):
                    unique[key] = item
                continue
            unique[key] = item
        candidates = list(unique.values())[:24]

        # Read only the most promising candidates to keep public hosting responsive.
        if request.deep:
            # Keep candidate type diversity: without this, metadata from one provider
            # could crowd all ordinary web pages out of the limited full-text fetches.
            preliminary = []
            for item in candidates:
                base_score, _, _ = calculate_match(text, " ".join([item.get("title", ""), item.get("snippet", "")]))
                preliminary.append((base_score, item))
            by_type: dict[str, list[tuple[float, dict]]] = defaultdict(list)
            for pair in preliminary:
                by_type[pair[1].get("type", "web")].append(pair)
            for group in by_type.values():
                group.sort(key=lambda pair: pair[0], reverse=True)
            type_order = [t for t in ("web", "jurnal", "preprint") if t in by_type]
            type_order.extend(t for t in by_type if t not in type_order)
            selected_pairs = []
            while len(selected_pairs) < 8 and any(by_type[t] for t in type_order):
                for candidate_type in type_order:
                    if by_type[candidate_type] and len(selected_pairs) < 8:
                        selected_pairs.append(by_type[candidate_type].pop(0))
            selected_ids = {id(item) for _, item in selected_pairs}
            if len(selected_pairs) < 8:
                for pair in sorted(preliminary, key=lambda x: x[0], reverse=True):
                    if id(pair[1]) not in selected_ids:
                        selected_pairs.append(pair)
                        selected_ids.add(id(pair[1]))
                    if len(selected_pairs) >= 8:
                        break
            to_read = [item for _, item in selected_pairs]
            read_results = await asyncio.gather(
                *(read_public_source(client, item.get("url", ""), item.get("pdf_url", "")) for item in to_read),
                return_exceptions=True,
            )
            for item, read_result in zip(to_read, read_results):
                if isinstance(read_result, str) and len(read_result) >= 150:
                    item["text"] = " ".join([item.get("text", ""), read_result])
                    item["full_text_checked"] = True
                    item["snippet"] = item.get("snippet") or read_result[:1200]

    suspect_tokens = token_spans(text)
    suspect_grams = ngrams_from_tokens(suspect_tokens)
    scored: list[dict] = []
    marks_by_word: dict[int, set[int]] = defaultdict(set)
    union_matches: set[str] = set()
    for item in candidates:
        haystack = " ".join([item.get("title", ""), item.get("snippet", ""), item.get("text", "")])
        score, matched_grams, matched_positions = calculate_match(text, haystack)
        if not matched_grams:
            continue
        union_matches.update(matched_grams)
        for start, end in matched_positions:
            for pos in range(start, end + 1):
                marks_by_word[pos].add(len(scored))
        source = {
            "index": len(scored),
            "title": item.get("title") or "Untitled source",
            "url": item.get("url") or "",
            "type": item.get("type") or "web",
            "authors": item.get("authors") or "",
            "venue": item.get("venue") or "",
            "year": item.get("year") or "",
            "score": score,
            "phrases": phrases_for(text, matched_positions),
            "full_text_checked": bool(item.get("full_text_checked")),
            "_matched_grams": matched_grams,
        }
        scored.append(source)

    scored.sort(key=lambda x: (x["score"], len(x["phrases"])), reverse=True)
    # Reassign stable indices after sorting, so segment highlighting matches cards.
    final_sources = []
    mark_sources: dict[int, set[int]] = defaultdict(set)
    for new_index, source in enumerate(scored[:12]):
        for gram in source["_matched_grams"]:
            # Mark all token windows with this source's matched 5-grams.
            n = 5 if len(suspect_tokens) >= 5 else len(suspect_tokens)
            for i in range(max(0, len(suspect_tokens) - n + 1)):
                gram_here = " ".join(suspect_tokens[i + j]["norm"] for j in range(n))
                if gram_here == gram:
                    for pos in range(i, i + n):
                        mark_sources[pos].add(new_index)
        source.pop("_matched_grams", None)
        source["index"] = new_index
        final_sources.append(source)

    all_grams = suspect_grams
    overall_score = round(len(union_matches) / max(len(all_grams), 1) * 100) if all_grams else 0
    overall_score = min(100, overall_score)
    segments = build_segments(text, mark_sources)
    elapsed = round(time.perf_counter() - started, 2)
    return {
        "overall_score": overall_score,
        "sources": final_sources,
        "segments": segments,
        "stats": {
            "candidates": len(candidates),
            "full_texts_read": sum(1 for item in candidates if item.get("full_text_checked")),
            "elapsed_s": elapsed,
        },
        "warnings": list(dict.fromkeys(warnings)),
        "queries": queries,
    }
