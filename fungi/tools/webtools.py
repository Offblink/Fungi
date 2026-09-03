"""Web tools: fetch a page as text, and web search.

Search uses Bing's server-rendered results page (brave.com was the original
choice but times out from mainland networks and rate-limits shared proxies,
returning HTTP 429). Results are parsed into titles, URLs, and snippets.

HTTP(S) proxies: urllib only honours environment proxies, and a stray NO_PROXY
env var makes getproxies() skip the Windows system (registry) proxy entirely.
_fetch therefore merges the registry proxy back in when the environment has
no real proxy configured, so a running system proxy (e.g. Clash) is used.
"""

import base64
import contextlib
import html as html_mod
import re
import urllib.error
import urllib.request
from urllib.parse import quote

WEB_TIMEOUT = 15
SEARCH_TIMEOUT = 10
WEB_TRUNCATE = 12000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", "", markup)
    text = re.sub(r"(?is)<style.*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)
    text = re.sub(r"(?m)^[ \t]+", "", text)
    return text.strip()


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [truncated {len(text) - limit} chars] ...\n\n{text[-half:]}"


def _proxies() -> dict:
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    registry = getattr(urllib.request, "getproxies_registry", None)
    if registry and not proxies:
        with contextlib.suppress(Exception):
            proxies = {k: v for k, v in registry().items() if k in ("http", "https")}
    return proxies


def _fetch(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies()))
    with opener.open(request, timeout=timeout) as resp:
        raw = resp.read()
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _unbing(url: str) -> str:
    """bing.com/ck/a?...&u=a1<base64url> tracking links -> real target URL."""
    m = re.search(r"[?&]u=a1([A-Za-z0-9_-]+)", url)
    if not m:
        return url
    raw = m.group(1)
    with contextlib.suppress(Exception):
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    return url


def tool_web(url: str) -> str:
    if not re.match(r"^https?://", url):
        url = f"https://{url}"
    try:
        text = _strip_html(_fetch(url, WEB_TIMEOUT))
    except urllib.error.HTTPError as exc:
        return f"ERROR: Web fetch failed: HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"ERROR: Web fetch failed: {reason}"
    return _truncate_middle(text, WEB_TRUNCATE)


def _search_bing(query: str) -> str:
    """Parsed Bing results: '1. Title\\n   URL\\n   snippet' per hit."""
    markup = _fetch(f"https://www.bing.com/search?q={quote(query)}&count=10", SEARCH_TIMEOUT)
    blocks = re.findall(r'(?is)<li[^>]*class="b_algo[^"]*"[^>]*>.*?</li>', markup)
    lines: list[str] = []
    for block in blocks[:8]:
        m = re.search(r'(?is)<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        url = _unbing(html_mod.unescape(m.group(1)))
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        sn = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", sn.group(1))) if sn else ""
        snippet = re.sub(r"\s+", " ", snippet).strip()
        lines.append(
            f"{len(lines) + 1}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else "")
        )
    return "\n\n".join(lines)


def _search_brave(query: str) -> str:
    """Fallback: full stripped Brave results page (works on some networks)."""
    text = _strip_html(_fetch(f"https://search.brave.com/search?q={quote(query)}", SEARCH_TIMEOUT))
    return _truncate_middle(text, WEB_TRUNCATE) if len(text) >= 50 else ""


def tool_web_search(query: str) -> str:
    engines = (_search_bing, _search_brave)
    last_error = ""
    for engine in engines:
        try:
            results = engine(query)
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            continue
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = str(getattr(exc, "reason", exc))
            continue
        if results:
            return results
    if last_error:
        return f"ERROR: Search failed: {last_error}"
    return f"(no results for '{query}')"
