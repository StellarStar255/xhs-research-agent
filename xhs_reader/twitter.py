"""Read public X posts through a separate, locally signed-in Chrome profile."""
import json
import re
import time
from contextlib import contextmanager
from urllib.parse import quote, urlsplit

from . import paths, scraper
from .persistence import atomic_json, file_lock, read_json

HOME = "https://x.com"
HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
STATUS_PATH = re.compile(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,20})(?:/(?:photo|video)/[0-9]+)?/?")
ACCOUNT = '[data-testid="SideNav_AccountSwitcher_Button"]'
LOGIN_FORM = ('input[autocomplete~="username"]', 'input[type="password"]')
POSTS = 'article[data-testid="tweet"]'
EMPTY = '[data-testid="emptyState"]'
PAGE_TIMEOUT = 20000
LOGIN_REQUIRED = "X_LOGIN_REQUIRED: 请点「登录 X」，在 Chrome 登录后再问一次。"
LIMITED = "X_LIMITED: X 暂时限制访问，请稍后再试。"


class BrowseError(RuntimeError):
    pass


def login_state():
    try:
        state = read_json(paths.DATA_DIR / "twitter-login.json")
    except (OSError, ValueError):
        return None
    return state if state in ("done", "expired") else None


def _save_login_state(state):
    if login_state() != state:
        atomic_json(paths.DATA_DIR / "twitter-login.json", state)


def post_url(value):
    """Accept a status link and discard its query, fragment and legacy hostname."""
    try:
        url = urlsplit(value)
        match = STATUS_PATH.fullmatch(url.path)
        if (url.scheme in ("https", "http") and url.hostname in HOSTS
                and url.username is None and url.port is None and match):
            return HOME + url.path[:match.end(1)], match[1]
    except ValueError:
        pass
    raise BrowseError("请提供 x.com 或 twitter.com 的原帖链接。")


@contextmanager
def browser(wait_s=30):
    root = paths.DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "twitter-browser.guard", timeout=wait_s):
        # X served the login page in visible Chrome while headless Chrome got HTTP 403.
        with scraper._browser_context(headless=False, profile_dir=root / "twitter-profile") as ctx:
            yield ctx.pages[0] if ctx.pages else ctx.new_page()


def _open(page, url):
    status = None

    def navigation_response(response):
        nonlocal status
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            status = response.status

    page.on("response", navigation_response)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if response:
            status = response.status
    except scraper.PlaywrightError:
        if status is None or status < 400:
            raise
    finally:
        page.remove_listener("response", navigation_response)
    if status == 429:
        raise BrowseError(LIMITED)
    if status is not None and status >= 400:
        raise BrowseError(f"X 网页无法打开（HTTP {status}）。")


def login(timeout_s=300):
    """Let the user enter credentials and complete any checks on x.com."""
    with browser(wait_s=0) as page:
        try:
            _open(page, HOME + "/home")
            account = page.locator(ACCOUNT + ":visible").first
            try:
                account.wait_for(timeout=3000)
                _save_login_state("done")
                return
            except scraper.PlaywrightTimeout:
                pass
            if not _login_required(page):
                _open(page, HOME + "/i/flow/login")
            try:
                account.wait_for(timeout=timeout_s * 1000)
                _save_login_state("done")
            except scraper.PlaywrightTimeout as exc:
                raise BrowseError("X 登录超时，请再试一次。") from exc
        except scraper.PlaywrightError as exc:
            if page.is_closed():
                raise BrowseError("X 登录窗口已关闭，请点「登录 X」重试。") from exc
            raise


def _login_required(page):
    path = urlsplit(page.url).path
    return (path in ("/login", "/account/access")
            or path.startswith(("/i/flow/login", "/i/jf/onboarding/"))
            or any(page.locator(selector + ":visible").first.is_visible() for selector in LOGIN_FORM))


def _ready(page, target, limited):
    visible_states = page.locator(", ".join(f"{state}:visible" for state in (EMPTY, *LOGIN_FORM)))
    ready = target.and_(page.locator(":visible")).or_(visible_states).first
    deadline = time.monotonic() + PAGE_TIMEOUT / 1000
    while True:
        if limited():
            raise BrowseError(LIMITED)
        if _login_required(page):
            _save_login_state("expired")
            raise BrowseError(LOGIN_REQUIRED)
        if ready.is_visible():
            return
        if time.monotonic() >= deadline:
            raise BrowseError("X 网页未能加载帖子，请在 Chrome 中确认页面可以打开。")
        page.wait_for_timeout(200)


@contextmanager
def _post_page(operation):
    with browser() as page:
        limited = False

        def on_response(response):
            nonlocal limited
            if response.status == 429:
                url = urlsplit(response.url)
                if (url.hostname in HOSTS | {"api.x.com", "api.twitter.com"}
                        and "/graphql/" in url.path and url.path.endswith("/" + operation)):
                    limited = True

        page.on("response", on_response)
        try:
            yield page, lambda: limited
        finally:
            page.remove_listener("response", on_response)


def _cards(articles, limit):
    rows = articles.evaluate_all("""articles => articles.map(article => {
        const time = article.querySelector('time');
        const link = time?.closest('a[href]');
        if (article.querySelector('[data-testid="icon-lock"], [data-testid="icon-verified-protected"]')) {
            return null;
        }
        return {
            url: link?.href || '',
            date: time?.getAttribute('datetime') || '',
            author: article.querySelector('[data-testid="User-Name"]')?.innerText || '',
            text: article.querySelector('[data-testid="tweetText"]')?.innerText || ''
        };
    })""")
    seen, results = set(), []
    for row in rows:
        if row is None:
            continue
        try:
            canonical, post_id = post_url(row["url"])
        except BrowseError:
            continue
        if post_id in seen:
            continue
        seen.add(post_id)
        # Keep 12 results, including headers and URLs, within the tool's 60k output window.
        results.append({**row, "url": canonical, "text": row["text"][:4000],
                        "author": row["author"][:300], "date": row["date"][:64]})
        if len(results) == limit:
            break
    return results


def search(keyword):
    with _post_page("SearchTimeline") as (page, limited):
        _open(page, HOME + "/search?q=" + quote(keyword, safe="") + "&src=typed_query")
        articles = page.locator(POSTS)
        _ready(page, articles, limited)
        results = _cards(articles, 12)
        if not results and not page.locator(EMPTY).first.is_visible():
            raise BrowseError("X 网页没有返回可识别的帖子，请稍后再试。")
        return results


def read(value):
    url, post_id = post_url(value)
    with _post_page("TweetDetail") as (page, limited):
        _open(page, url)
        # A thread can include parent posts, quotes, and replies. Match the
        # timestamp's own status link before returning the requested post.
        timestamp = page.locator(
            f"xpath=(.//time)[1]/ancestor::a[1][contains(concat(@href, '?'), '/status/{post_id}?')"
            f" or contains(concat(@href, '?'), '/status/{post_id}/?')]")
        articles = page.locator(POSTS).filter(has=timestamp)
        _ready(page, articles, limited)
        results = _cards(articles, 12)
        for row in results:
            if post_url(row["url"])[1] == post_id:
                return [row]
        raise BrowseError("找不到这篇 X 帖子，可能已删除或无法访问。")


def digest(rows):
    header = f"X 帖子数: {len(rows)}\n"
    result = json.dumps(rows, ensure_ascii=False)
    if len(result) > 59000:
        result = json.dumps([{**row, "text": row["text"][:600], "author": row["author"][:150]}
                             for row in rows], ensure_ascii=False)
    return header + result
