import json
import os
import re
from html.parser import HTMLParser
from typing import Any, List, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request

from fastapi import HTTPException

from config import (
    IMPORT_ALLOWED_EXTS,
    IMPORT_BLOCKED_EXTS,
    IMPORT_BROWSER_USER_AGENT,
    IMPORT_EXT_PRIORITY,
    IMPORT_HTML_MAX_BYTES,
    IMPORT_TIMEOUT_SECONDS,
    IMPORT_USER_AGENT,
)
from file_utils import is_json_content_type
from safe_http import safe_urlopen
from schemas import ImportRequest


class CredentialRefusal(HTTPException):
    """A refusal whose cause is the credential this server sent, not the site's answer.

    The detail is exactly what the same branch answered before this class
    existed, so a client that pinned the string keeps working; the status is
    the vendor's own except for a 401, which the paragraph below explains. The
    class is what lets a caller tell "the cookie this server sent is dead" from
    "the site refused an anonymous request" without parsing English.
    ``project_import._is_gated`` matches it with ``isinstance``, the same way
    ``entry_policy.is_entry_failure`` matches ``AssetFilenameError``.

    Every detail a subclass path raises composes one site's remedy through that
    site's composer (``_thingiverse_cookie_detail`` or
    ``_makerworld_cookie_detail``), so each remedy is written once.

    The vendor's status is carried through unchanged except for 401, which is
    reported as 403 on purpose: the web client throws ``UnauthorizedError`` on a
    401 before it reads ``detail``, so a 401 would hide the remedy. The remap
    lives here, in the one place every refusal passes through, so no site that
    raises this class can forget it.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code=403 if status_code == 401 else status_code, detail=detail)


# One remedy, formatted twice. The paste lives under Settings > Import sources and
# wins over the environment variable (see resolve_thingiverse_cookie and
# resolve_makerworld_cookie), so naming only one of them sends half the operators
# to a control they cannot use. MakerWorld's remedy is this sentence with the one
# variable renamed, which is what the tests pin: the two cannot drift apart.
_COOKIE_REMEDY_TEMPLATE = (
    "Paste the Cookie header from a signed-in browser session under Settings > Import sources, "
    "or set the {variable} environment variable on the server and restart the API. "
    "Nothing was changed."
)

# The bound on every piece of vendor-controlled text that lands in a detail: a
# chatty or hostile body must not push the remedy off the screen. Both sites'
# composers take it, so neither can bury the remedy the other one names.
_VENDOR_TEXT_BOUND = 300

THINGIVERSE_COOKIE_REMEDY = _COOKIE_REMEDY_TEMPLATE.format(variable="THINGIVERSE_COOKIE")
MAKERWORLD_COOKIE_REMEDY = _COOKIE_REMEDY_TEMPLATE.format(variable="MAKERWORLD_COOKIE")


def makerworld_html_headers(referer: Optional[str] = None, cookie: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": IMPORT_BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": "\"Not A(Brand\";v=\"99\", \"Google Chrome\";v=\"121\", \"Chromium\";v=\"121\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    return headers


def makerworld_api_headers(referer: Optional[str] = None, nonce: Optional[str] = None, cookie: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": IMPORT_BROWSER_USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "X-BBL-Client-Type": "web",
        "X-BBL-Client-Version": "00.00.00.01",
        "X-BBL-App-Source": "makerworld",
        "X-BBL-Client-Name": "MakerWorld",
    }
    if referer:
        headers["Referer"] = referer
    if nonce:
        headers["X-Nonce"] = nonce
    if cookie:
        headers["Cookie"] = cookie
    return headers


def resolve_makerworld_cookie(body: ImportRequest) -> Optional[str]:
    raw = (body.makerworld_cookie or os.getenv("MAKERWORLD_COOKIE", "")).strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    value = lines[0]
    for line in lines:
        if line.lower().startswith("cookie:"):
            value = line[len("cookie:"):].strip()
            break
    return value or None


class _ThingiverseCookieBody(Protocol):
    """The one attribute ``resolve_thingiverse_cookie`` reads off a request body.

    Structural on purpose: ``ImportRequest`` and the enrich request model both
    carry the field, and neither should be a type lie just to share the resolver.
    """

    thingiverse_cookie: Optional[str]


def resolve_thingiverse_cookie(body: _ThingiverseCookieBody) -> Optional[str]:
    raw = (body.thingiverse_cookie or os.getenv("THINGIVERSE_COOKIE", "")).strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    value = lines[0]
    for line in lines:
        if line.lower().startswith("cookie:"):
            value = line[len("cookie:"):].strip()
            break
    return value or None


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if not value:
                continue
            key_lower = key.lower()
            if key_lower in {"href", "src", "data-download", "data-download-url", "data-url", "data-file", "data-href"}:
                self.links.append(value)


def score_download_url(url: str) -> int:
    lower = url.lower()
    score = 0
    if "download" in lower:
        score += 6
    if "files" in lower:
        score += 2
    for idx, ext in enumerate(IMPORT_EXT_PRIORITY):
        if ext in lower:
            score += (len(IMPORT_EXT_PRIORITY) - idx) * 10
    return score


def is_potential_url(value: str) -> bool:
    if re.search(r"\s", value):
        return False
    if re.search(r"[<>{}\"\\^`]", value):
        return False
    return True


def contains_allowed_ext(url: str) -> bool:
    lower = url.lower()
    for ext in IMPORT_ALLOWED_EXTS:
        if re.search(re.escape(ext) + r"(?:$|[?#&])", lower):
            return True
    return False


def is_allowed_download_candidate(url: str) -> bool:
    lower = url.lower()
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext:
        if ext in IMPORT_ALLOWED_EXTS:
            return True
        if ext in IMPORT_BLOCKED_EXTS:
            return False
        if "download" in lower:
            return True
        return contains_allowed_ext(lower)
    if contains_allowed_ext(lower):
        return True
    return "download" in lower


def find_download_url(html: str, base_url: str) -> Optional[str]:
    parser = LinkCollector()
    try:
        parser.feed(html)
    except Exception:
        pass
    links = list(parser.links)
    links.extend(re.findall(r"https?://[^\s\"'<>]+", html, re.IGNORECASE))

    candidates: List[str] = []
    seen = set()
    for link in links:
        link = (link or "").strip()
        if not link or link.startswith("#"):
            continue
        if link.lower().startswith(("javascript:", "mailto:")):
            continue
        abs_url = urljoin(base_url, link)
        parsed = urlparse(abs_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        if is_allowed_download_candidate(abs_url):
            candidates.append(abs_url)

    if not candidates:
        return None
    candidates.sort(key=score_download_url, reverse=True)
    return candidates[0]


def normalize_candidate_url(raw: str, base_url: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value:
        return None
    if not is_potential_url(value):
        return None
    if value.startswith("//"):
        value = f"https:{value}"
    if value.startswith(("http://", "https://")):
        return value if is_allowed_download_candidate(value) else None
    if value.startswith("/"):
        abs_url = urljoin(base_url, value)
        return abs_url if is_allowed_download_candidate(abs_url) else None
    if "/" in value:
        abs_url = urljoin(base_url, value)
        return abs_url if is_allowed_download_candidate(abs_url) else None
    return None


def find_download_url_in_json(data: Any, base_url: str) -> Optional[str]:
    candidates: List[str] = []
    seen = set()
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            if current in seen:
                continue
            seen.add(current)
            url = normalize_candidate_url(current, base_url)
            if url:
                candidates.append(url)
    if not candidates:
        return None
    candidates.sort(key=score_download_url, reverse=True)
    return candidates[0]


def extract_next_data_json(html: str) -> Optional[Any]:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    raw = (match.group(1) or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def makerworld_design_id_from_next_data(data: Any) -> Optional[str]:
    try:
        design = data.get("props", {}).get("pageProps", {}).get("design", {})
        design_id = design.get("id")
        return str(design_id) if design_id else None
    except AttributeError:
        return None


def makerworld_instance_id_from_next_data(data: Any) -> Optional[str]:
    try:
        design = data.get("props", {}).get("pageProps", {}).get("design", {})
        default_instance = design.get("defaultInstanceId")
        if default_instance:
            return str(default_instance)
        instances = design.get("instances") or []
        if isinstance(instances, list):
            for inst in instances:
                if isinstance(inst, dict):
                    inst_id = inst.get("id")
                    if inst_id:
                        return str(inst_id)
        return None
    except AttributeError:
        return None


def makerworld_nonce_from_next_data(data: Any) -> Optional[str]:
    try:
        nonce = data.get("props", {}).get("pageProps", {}).get("x-nonce")
        return nonce if isinstance(nonce, str) and nonce.strip() else None
    except AttributeError:
        return None


def makerworld_model_id_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = re.search(r"/models/(\d+)", parsed.path or "")
    return match.group(1) if match else None


def fetch_json_from_url(url: str, referer: Optional[str] = None, headers: Optional[dict] = None) -> Optional[Any]:
    request_headers = {"User-Agent": IMPORT_USER_AGENT, "Accept": "application/json"}
    if referer:
        request_headers["Referer"] = referer
    if headers:
        request_headers.update(headers)
    req = Request(url, headers=request_headers)
    try:
        resp = safe_urlopen(req, timeout=IMPORT_TIMEOUT_SECONDS)
    except HTTPError:
        return None
    except URLError:
        return None

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if not is_json_content_type(content_type):
            trimmed = raw.lstrip()
            if not trimmed.startswith((b"{", b"[")):
                return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


def parse_json_error_message(raw: bytes) -> Optional[str]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("error", "detail", "message"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_download_url_from_response(data: Any, base_url: str) -> Optional[str]:
    if isinstance(data, dict):
        for key in ("url", "downloadUrl", "download_url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("url", "downloadUrl", "download_url"):
                value = inner.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return find_download_url_in_json(data, base_url)


def _makerworld_bounded_message(message: Optional[str], fallback: str = "MakerWorld blocked the request") -> str:
    """The vendor's own words, bounded, carrying no period of their own.

    The bound is taken FIRST, on the raw vendor string, and periods are stripped
    from that bounded result only. The vendor controls the punctuation, so
    stripping first would let a run of periods survive a truncation that lands
    inside it and the composed sentence would read "Forbidden..". The fallback
    carries no period either, so the lead ends with exactly one either way.
    """
    text = (message or "").strip()
    if not text:
        return fallback
    bounded = text[:_VENDOR_TEXT_BOUND].rstrip(".")
    return bounded or fallback


def _makerworld_bounded_id(value: str) -> str:
    """A page-supplied id quoted in a message is bounded; the request keeps the raw one."""
    return value[:64]


def _makerworld_cookie_detail(lead: str) -> str:
    """The one MakerWorld composer: a lead sentence, then the remedy both surfaces share."""
    return f"{lead} {MAKERWORLD_COOKIE_REMEDY}"


# The Settings card's own promise, said once here so a refusal cannot contradict
# it: MakerWorld's cookie is a rate-limit workaround for FREE models, and paid or
# account-gated files are never fetched (see ImportsSection.tsx). It belongs in
# the lead, before the remedy, because a paid model whose ``gated`` flag failed to
# parse reaches this refusal and must not be told a cookie would have helped.
_MAKERWORLD_PAID_HEDGE = (
    "The cookie is only a rate-limit workaround for free models: paid or account-gated files are never fetched."
)


def _refuse_makerworld_credential(err: HTTPError, makerworld_cookie: Optional[str], subject: str) -> None:
    """Refuse a download-API answer that blames the cookie, or answer ``None`` otherwise.

    Two things about the order here are load-bearing. Without a cookie this
    returns before ``err.read()`` is called at all, so the caller's
    ``find_download_url`` rescue and the generic 400 are byte-identical by
    construction rather than by assertion. And the vendor's own code stays in the
    lead, because the status a client sees is not always the vendor's: a 401
    arrives as 403 (see ``CredentialRefusal``), so the lead is the one place the
    vendor's own code still shows.
    """
    if not makerworld_cookie or err.code not in {401, 403, 429}:
        return None
    try:
        message = parse_json_error_message(err.read())
    except Exception:
        message = None
    vendor = _makerworld_bounded_message(message)
    lead = f"MakerWorld refused the download API for {subject} (HTTP {err.code}): {vendor}. {_MAKERWORLD_PAID_HEDGE}"
    raise CredentialRefusal(status_code=err.code, detail=_makerworld_cookie_detail(lead))


def fetch_makerworld_instance_download_url(
    instance_id: str,
    page_url: str,
    makerworld_cookie: Optional[str],
) -> Optional[str]:
    """One download-API answer for the design's default instance, or ``None``.

    A 401/403/429 while a cookie was sent is the dead-credential answer
    ``_refuse_makerworld_credential`` turns into a refusal; without a cookie the
    caller's rescue still runs exactly as before.
    """
    api_url = (
        "https://makerworld.com/api/v1/design-service/instance/"
        f"{instance_id}/f3mf?type=download&fileType=3mfstl"
    )
    req = Request(api_url, headers=makerworld_api_headers(referer=page_url, cookie=makerworld_cookie))
    try:
        resp = safe_urlopen(req, timeout=IMPORT_TIMEOUT_SECONDS)
    except HTTPError as err:
        _refuse_makerworld_credential(
            err, makerworld_cookie, f"the design's default instance {_makerworld_bounded_id(instance_id)}"
        )
        return None
    except URLError:
        return None

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if not is_json_content_type(content_type):
            trimmed = raw.lstrip()
            if not trimmed.startswith((b"{", b"[")):
                return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return extract_download_url_from_response(data, api_url)


def fetch_makerworld_model_download_url(
    model_id: str,
    page_url: str,
    nonce: Optional[str],
    makerworld_cookie: Optional[str],
) -> Optional[str]:
    """One download-API answer for ``model_id``, or ``None`` when it carries no URL.

    A 401/403/429 while a cookie was sent is the dead-credential answer
    ``_refuse_makerworld_credential`` turns into a refusal; without a cookie the
    four anonymous candidates and the caller's rescue still run exactly as before.
    """
    api_url = f"https://makerworld.com/api/v1/models/{model_id}/download"
    req = Request(api_url, headers=makerworld_api_headers(referer=page_url, nonce=nonce, cookie=makerworld_cookie))
    try:
        resp = safe_urlopen(req, timeout=IMPORT_TIMEOUT_SECONDS)
    except HTTPError as err:
        _refuse_makerworld_credential(err, makerworld_cookie, f"design {_makerworld_bounded_id(model_id)}")
        return None
    except URLError:
        return None

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if not is_json_content_type(content_type):
            trimmed = raw.lstrip()
            if not trimmed.startswith((b"{", b"[")):
                return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return extract_download_url_from_response(data, api_url)


def resolve_makerworld_download_url(
    html: str,
    page_url: str,
    makerworld_cookie: Optional[str],
) -> Optional[str]:
    next_data = extract_next_data_json(html)
    design_id = None
    nonce = None
    instance_id = None
    if next_data:
        url = find_download_url_in_json(next_data, page_url)
        if url:
            return url
        design_id = makerworld_design_id_from_next_data(next_data)
        nonce = makerworld_nonce_from_next_data(next_data)
        instance_id = makerworld_instance_id_from_next_data(next_data)

    if instance_id:
        url = fetch_makerworld_instance_download_url(instance_id, page_url, makerworld_cookie)
        if url:
            return url

    model_id = design_id or makerworld_model_id_from_url(page_url)
    if not model_id:
        return None
    url = fetch_makerworld_model_download_url(model_id, page_url, nonce, makerworld_cookie)
    if url:
        return url
    api_candidates = [
        f"https://makerworld.com/api/v1/models/{model_id}",
        f"https://makerworld.com/api/v1/models/{model_id}/files",
        f"https://makerworld.com/api/v1/model/{model_id}",
        f"https://makerworld.com/api/v1/model/{model_id}/files",
    ]
    for api_url in api_candidates:
        data = fetch_json_from_url(api_url, referer=page_url)
        if not data:
            continue
        url = find_download_url_in_json(data, api_url)
        if url:
            return url
    return None


def is_printables_page_host(host: str) -> bool:
    host = (host or "").lower()
    return host in {"printables.com", "www.printables.com"}


def is_makerworld_page_host(host: str) -> bool:
    """Exact/subdomain match only.

    ``host.endswith("makerworld.com")`` also matches ``evil-makerworld.com`` and
    ``notmakerworld.com``, which would send the user's session cookie there.
    """
    host = (host or "").lower()
    return host == "makerworld.com" or host.endswith(".makerworld.com")


def printables_html_headers(referer: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": IMPORT_BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def printables_model_id_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = re.search(r"/model/(\d+)", parsed.path or "")
    return match.group(1) if match else None


def fetch_printables_graphql(query: str, variables: dict, referer: Optional[str]) -> Optional[Any]:
    payload = {"query": query, "variables": variables or {}}
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": IMPORT_BROWSER_USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.printables.com",
    }
    if referer:
        headers["Referer"] = referer
    req = Request("https://api.printables.com/graphql/", data=data, headers=headers)
    try:
        resp = safe_urlopen(req, timeout=IMPORT_TIMEOUT_SECONDS)
    except HTTPError as err:
        message = None
        try:
            message = parse_json_error_message(err.read())
        except Exception:
            message = None
        if err.code in {401, 403, 429}:
            raise HTTPException(status_code=err.code or 400, detail=message or "Printables request failed") from err
        return None
    except URLError:
        return None

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


def parse_printables_error_messages(errors: Any) -> Optional[str]:
    if not isinstance(errors, list):
        return None
    messages: List[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        values = err.get("messages")
        if isinstance(values, list):
            messages.extend([value for value in values if isinstance(value, str) and value.strip()])
        elif isinstance(values, str) and values.strip():
            messages.append(values.strip())
    return ", ".join(messages) if messages else None


def select_best_download_link(links: List[str]) -> Optional[str]:
    candidates = [link for link in links if is_allowed_download_candidate(link)]
    if not candidates:
        return None
    candidates.sort(key=score_download_url, reverse=True)
    return candidates[0]


def extract_printables_ids(items: Any) -> List[str]:
    ids: List[str] = []
    if not isinstance(items, list):
        return ids
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("id")
        if value is None:
            continue
        ids.append(str(value))
    return ids


def fetch_printables_model_meta(print_id: str, page_url: str) -> Optional[dict]:
    query = """
    query ($id: ID!) {
      print(id: $id) {
        id
        downloadPacks { id fileType }
        stls { id name }
      }
    }
    """
    data = fetch_printables_graphql(query, {"id": print_id}, referer=page_url)
    if not isinstance(data, dict):
        return None
    model = data.get("data", {}).get("print")
    return model if isinstance(model, dict) else None


def fetch_printables_download_output(print_id: str, variables: dict, page_url: str) -> Optional[dict]:
    mutation = """
    mutation ($printId: ID!, $source: DownloadSourceEnum!, $fileType: DownloadFileTypeEnum, $id: ID, $files: [DownloadFileInput!]) {
      getDownloadLink(printId: $printId, source: $source, fileType: $fileType, id: $id, files: $files) {
        ok
        errors { field messages code }
        output { link files { id link fileType } }
      }
    }
    """
    data = fetch_printables_graphql(mutation, variables, referer=page_url)
    if not isinstance(data, dict):
        return None
    result = data.get("data", {}).get("getDownloadLink")
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False:
        message = parse_printables_error_messages(result.get("errors"))
        if message:
            raise HTTPException(status_code=400, detail=message)
        return None
    output = result.get("output")
    return output if isinstance(output, dict) else None


def resolve_printables_download_url(page_url: str) -> Optional[str]:
    print_id = printables_model_id_from_url(page_url)
    if not print_id:
        return None
    meta = fetch_printables_model_meta(print_id, page_url)
    if not meta:
        return None
    packs = meta.get("downloadPacks") if isinstance(meta, dict) else None
    pack_id = None
    if isinstance(packs, list):
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            if pack.get("fileType") == "MODEL_FILES" and pack.get("id"):
                pack_id = str(pack.get("id"))
                break
        if not pack_id and packs:
            for pack in packs:
                if isinstance(pack, dict) and pack.get("id"):
                    pack_id = str(pack.get("id"))
                    break
    if pack_id:
        output = fetch_printables_download_output(
            print_id,
            {"printId": print_id, "source": "model_detail", "fileType": "pack", "id": pack_id},
            page_url,
        )
        if output:
            links = [link for link in [output.get("link")] if isinstance(link, str)]
            for entry in output.get("files") or []:
                if isinstance(entry, dict) and isinstance(entry.get("link"), str):
                    links.append(entry["link"])
            best = select_best_download_link(links)
            if best:
                return best
    stl_ids = extract_printables_ids(meta.get("stls"))
    if stl_ids:
        output = fetch_printables_download_output(
            print_id,
            {"printId": print_id, "source": "model_detail", "files": [{"fileType": "stl", "ids": stl_ids}]},
            page_url,
        )
        if output:
            links = [link for link in [output.get("link")] if isinstance(link, str)]
            for entry in output.get("files") or []:
                if isinstance(entry, dict) and isinstance(entry.get("link"), str):
                    links.append(entry["link"])
            best = select_best_download_link(links)
            if best:
                return best
    return None


# A thing page is one HTML document, not a file: 10 s is generous for it while a
# hung vendor still fails a human's click quickly.
ENRICH_TIMEOUT_SECONDS = 10
# The canonical thing URL answers 200 directly (measured), so a redirect is a site
# change we do not follow: refusing it keeps this call to one request.
ENRICH_MAX_REDIRECTS = 0


def is_thingiverse_page_host(host: str) -> bool:
    host = (host or "").lower()
    return host in {"thingiverse.com", "www.thingiverse.com"}


def thingiverse_html_headers(referer: Optional[str] = None, cookie: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": IMPORT_BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    return headers


def thingiverse_api_headers(
    referer: Optional[str] = None,
    cookie: Optional[str] = None,
    token: Optional[str] = None,
) -> dict:
    headers = {
        "User-Agent": IMPORT_BROWSER_USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.thingiverse.com",
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def thingiverse_thing_id_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = re.search(r"thing:(\d+)", parsed.path or "", re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"/things/(\d+)", parsed.path or "", re.IGNORECASE)
    return match.group(1) if match else None


# --- the cookie refusals ---------------------------------------------------
#
# One lead per observed shape, and every one of them is joined to
# THINGIVERSE_COOKIE_REMEDY by _thingiverse_cookie_detail, so no sentence here
# restates the remedy. Only the no-token lead may diagnose the credential: a
# body this server could not read is not evidence that the cookie is dead, only
# that it went unchecked.

_VIEW_TOKEN_ABSENT = (
    "Thingiverse answered the view-token request with an empty body, so this server could not check the cookie it sent."
)
_VIEW_TOKEN_UNVERIFIABLE = (
    "Thingiverse answered the view-token request with a body this server cannot read, which is what a Cloudflare "
    "challenge looks like, so the cookie it sent went unchecked."
)
_VIEW_TOKEN_TOO_LARGE = (
    f"Thingiverse's view-token answer was larger than the import cap ({IMPORT_HTML_MAX_BYTES} bytes), so this "
    "server did not read it and the cookie it sent went unchecked."
)
_VIEW_TOKEN_NO_TOKEN = (
    "Thingiverse answered the view-token request without a token, so the cookie it was sent is signed-out or expired."
)


def _thingiverse_cookie_detail(lead: str) -> str:
    """The one composer: a lead sentence, then the remedy both surfaces share."""
    return f"{lead} {THINGIVERSE_COOKIE_REMEDY}"


def _vendor_prefix(message: Optional[str], fallback: str = "Thingiverse blocked the request.") -> str:
    """The vendor's own words, bounded, or ``fallback`` when it gave none.

    ``parse_json_error_message`` already stripped the vendor text, so the only
    thing it needs is a bound: a JSON body must not bury the remedy that follows
    it. Both callers take the bound, so the files step cannot push the remedy off
    the screen any more than the view-token step can.
    """
    return message[:_VENDOR_TEXT_BOUND] if message else fallback


def _refuse_credential(cookie_sent: bool, lead: str) -> None:
    """Refuse, or answer ``None`` when no cookie was implicated.

    Every shape this guards is evidence about the credential only if one was
    actually sent; without one the caller's ``find_download_url`` rescue stays
    exactly as it was, which is what keeps a public page importable.
    """
    if not cookie_sent:
        return None
    raise CredentialRefusal(status_code=403, detail=_thingiverse_cookie_detail(lead))


def fetch_thingiverse_json(
    url: str,
    page_url: str,
    thingiverse_cookie: Optional[str],
    token: Optional[str],
    thing_id: str,
) -> Optional[Any]:
    """One download-API answer for ``thing_id``, or ``None`` when it carries no URL.

    ``thing_id`` is here only to name the thing in a refusal: a 401/403/429 from
    the API while a cookie was sent is the same dead-credential answer the
    view-token step gives, and it is useless to the operator without the thing it
    was reading. Without a cookie the vendor's word stands unchanged.
    """
    cookie_sent = bool(thingiverse_cookie)
    req = Request(url, headers=thingiverse_api_headers(referer=page_url, cookie=thingiverse_cookie, token=token))
    try:
        resp = safe_urlopen(req, timeout=IMPORT_TIMEOUT_SECONDS)
    except HTTPError as err:
        message = None
        try:
            message = parse_json_error_message(err.read())
        except Exception:
            message = None
        if err.code in {401, 403, 429}:
            vendor = _vendor_prefix(message, "Thingiverse request failed")
            if not cookie_sent:
                raise HTTPException(status_code=err.code or 400, detail=vendor) from err
            raise CredentialRefusal(
                status_code=err.code or 400,
                detail=_thingiverse_cookie_detail(
                    f"Thingiverse refused the download API for thing {thing_id} (HTTP {err.code}): {vendor}"
                ),
            ) from err
        return None
    except URLError:
        return None

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if not is_json_content_type(content_type):
            trimmed = raw.lstrip()
            if not trimmed.startswith((b"{", b"[")):
                return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


def fetch_thingiverse_view_token(page_url: str, thingiverse_cookie: Optional[str]) -> Optional[str]:
    """The download-API view token, or ``None`` when no cookie was implicated.

    With a cookie, every answer this cannot use becomes a refusal carrying the
    shared remedy, because the operator's next step is the same for all of them:
    re-paste the cookie. Without one they still answer ``None``, so the caller's
    ``find_download_url`` rescue survives - these shapes are evidence about a
    credential that was actually sent. A transport failure is NOT one of them: it
    answers a 502 that carries no remedy, because a server that could not be
    reached says nothing about the cookie.
    """
    api_url = "https://www.thingiverse.com/api/v2/auth/view"
    cookie_sent = bool(thingiverse_cookie)
    req = Request(api_url, headers=thingiverse_api_headers(referer=page_url, cookie=thingiverse_cookie))
    try:
        resp = safe_urlopen(req, timeout=IMPORT_TIMEOUT_SECONDS)
    except HTTPError as err:
        message = None
        try:
            message = parse_json_error_message(err.read())
        except Exception:
            message = None
        if err.code in {401, 403, 429}:
            detail = _thingiverse_cookie_detail(_vendor_prefix(message))
            if not cookie_sent:
                raise HTTPException(status_code=err.code or 400, detail=detail) from err
            raise CredentialRefusal(status_code=err.code or 400, detail=detail) from err
        if not cookie_sent:
            return None
        raise HTTPException(
            status_code=502,
            detail=(
                f"Thingiverse answered HTTP {err.code} for the view-token request, which this import does not "
                "read. Nothing was changed."
            ),
        ) from err
    except URLError as err:
        if not cookie_sent:
            return None
        raise HTTPException(
            status_code=502,
            detail=(
                "Thingiverse did not answer in time or could not be reached for the view-token request. Check "
                "this server's network access and try again; nothing was changed."
            ),
        ) from err

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            return _refuse_credential(cookie_sent, _VIEW_TOKEN_TOO_LARGE)
        content_type = resp.headers.get("Content-Type", "")
        if not is_json_content_type(content_type):
            trimmed = raw.lstrip()
            if not trimmed.startswith((b"{", b"[")):
                return _refuse_credential(cookie_sent, _VIEW_TOKEN_UNVERIFIABLE)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            lead = _VIEW_TOKEN_ABSENT if not raw.strip() else _VIEW_TOKEN_UNVERIFIABLE
            return _refuse_credential(cookie_sent, lead)
    if not isinstance(data, dict):
        return _refuse_credential(cookie_sent, _VIEW_TOKEN_UNVERIFIABLE)
    token = data.get("access") or data.get("token") or data.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    inner = data.get("data")
    if isinstance(inner, dict):
        token = inner.get("access") or inner.get("token") or inner.get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return _refuse_credential(cookie_sent, _VIEW_TOKEN_NO_TOKEN)


def fetch_thingiverse_thing_page(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
    """GET the public thing page; the ONLY outbound call the enrich action makes.

    It never retries: a rate limit is a refusal a human acts on, not a loop, and
    this function is reached only from ``bundle_service.enrich_bundles``, never
    from the report. Every status this call can meet becomes ONE
    ``HTTPException`` written for an operator, and its cookie refusals reuse
    ``THINGIVERSE_COOKIE_REMEDY``, so they name the Settings paste and the
    environment variable alike. A status with no mapping of its own is
    reported as a 502 rather than escaping as a raw ``HTTPError``, because the
    batch must keep its other answers and its single cache write.
    """
    url = f"https://www.thingiverse.com/thing:{thing_id}"
    req = Request(url, headers=thingiverse_html_headers(referer=None, cookie=thingiverse_cookie))
    try:
        resp = safe_urlopen(req, timeout=ENRICH_TIMEOUT_SECONDS, max_redirects=ENRICH_MAX_REDIRECTS)
    except HTTPError as err:
        if err.code == 429:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Thingiverse is rate limiting this server (HTTP 429) for thing {thing_id}. "
                    f"{THINGIVERSE_COOKIE_REMEDY}"
                ),
            ) from err
        if err.code in {401, 403}:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Thingiverse refused the page (HTTP {err.code}) for thing {thing_id}; a Cloudflare challenge "
                    f"looks like this. {THINGIVERSE_COOKIE_REMEDY}"
                ),
            ) from err
        if err.code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Thingiverse has no such thing: it answered HTTP 404 for thing {thing_id}.",
            ) from err
        # Any other status - a 5xx, or a 4xx this action has no reading for - is
        # the vendor failing the request, not a bug here: report it as one so the
        # batch keeps the pages that answered.
        raise HTTPException(
            status_code=502,
            detail=(
                f"Thingiverse answered HTTP {err.code} for thing {thing_id}, which this action does not read. "
                "Nothing was changed."
            ),
        ) from err
    except HTTPException as err:
        if err.status_code != 400:
            raise
        # `safe_urlopen(max_redirects=0)` reports a redirect as a 400; say what it
        # means here rather than leaving the operator with a generic message.
        raise HTTPException(
            status_code=400,
            detail=(
                f"The Thingiverse page for thing {thing_id} redirected, and this action does not follow "
                "redirects. Nothing was changed."
            ),
        ) from err
    except (URLError, TimeoutError, OSError) as err:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Thingiverse did not answer in time or could not be reached for thing {thing_id}. Check this "
                "server's network access and try again; nothing was changed."
            ),
        ) from err

    with resp:
        raw = resp.read(IMPORT_HTML_MAX_BYTES + 1)
        if len(raw) > IMPORT_HTML_MAX_BYTES:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"The Thingiverse page for thing {thing_id} was larger than the import cap "
                    f"({IMPORT_HTML_MAX_BYTES} bytes), so it was not read. Nothing was changed."
                ),
            )
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def thingiverse_no_metadata_detail(thing_id: str) -> str:
    """Why a 200 page with no thing metadata is a refusal, and what to do next.

    Thingiverse answers a nonexistent id with a generic 200 page (measured), so
    absent metadata is the only signal there is: the id may name a deleted or
    private thing, or the site may have served a challenge page instead. The
    remedy is ``THINGIVERSE_COOKIE_REMEDY``, the same one the import path and
    the fetch refusals use, so it names the Settings paste as well as the
    environment variable.
    """
    return (
        f"Thingiverse answered thing {thing_id} with a page that carried no creator or licence. The thing may "
        "not exist any more (deleted or private), or Thingiverse may have answered a challenge page instead. "
        f"{THINGIVERSE_COOKIE_REMEDY}"
    )


def resolve_thingiverse_download_url(page_url: str, thingiverse_cookie: Optional[str]) -> Optional[str]:
    thing_id = thingiverse_thing_id_from_url(page_url)
    if not thing_id:
        return None
    token = fetch_thingiverse_view_token(page_url, thingiverse_cookie)
    if not token:
        # Reachable only without a cookie: with one, fetch_thingiverse_view_token
        # answers a refusal or a 502 instead of None, so this sentence can never
        # be the answer to a live credential.
        raise HTTPException(
            status_code=403,
            detail=(
                "Thingiverse requires a session cookie to fetch download links. "
                f"{THINGIVERSE_COOKIE_REMEDY}"
            ),
        )
    api_candidates = [
        f"https://www.thingiverse.com/api/v2/things/{thing_id}/files",
        f"https://www.thingiverse.com/api/v2/things/{thing_id}",
    ]
    for api_url in api_candidates:
        data = fetch_thingiverse_json(api_url, page_url, thingiverse_cookie, token, thing_id)
        if not data:
            continue
        url = find_download_url_in_json(data, api_url)
        if url:
            return url
    return f"https://www.thingiverse.com/thing:{thing_id}/zip"
