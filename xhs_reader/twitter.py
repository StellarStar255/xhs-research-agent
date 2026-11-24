"""Read public X posts through a separate, locally signed-in Chrome profile."""
import re
from contextlib import contextmanager
from urllib.parse import quote, urlsplit

from . import paths, scraper
from .persistence import file_lock

HOME = "https://x.com"
HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
STATUS_PATH = re.compile(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,20})(?:/(?:photo|video)/[0-9]+)?/?")
ACCOUNT = '[data-testid="SideNav_AccountSwitcher_Button"]'
LOGIN_FORM = ('input[autocomplete~="username"]', 'input[type="password"]')
POSTS = 'article[data-testid="tweet"]'
EMPTY = '[data-testid="emptyState"]'
PAGE_TIMEOUT = 20000


class BrowseError(RuntimeError):
    pass


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
        raise BrowseError("X_LIMITED: X 暂时限制访问，请稍后再试。")
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
                return
            except scraper.PlaywrightTimeout:
                pass
            if not _login_required(page):
                _open(page, HOME + "/i/flow/login")
            try:
                account.wait_for(timeout=timeout_s * 1000)
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


def _ready(page, target):
    try:
        visible_states = page.locator(", ".join(f"{state}:visible" for state in (EMPTY, *LOGIN_FORM)))
        target.and_(page.locator(":visible")).or_(visible_states).first.wait_for(
            state="visible", timeout=PAGE_TIMEOUT)
    except scraper.PlaywrightTimeout as exc:
        if _login_required(page):
            raise BrowseError("X_LOGIN_REQUIRED: 请点「登录 X」，在 Chrome 登录后再问一次。") from exc
        raise BrowseError("X 网页未能加载帖子，请在 Chrome 中确认页面可以打开。") from exc
    if _login_required(page):
        raise BrowseError("X_LOGIN_REQUIRED: 请点「登录 X」，在 Chrome 登录后再问一次。")


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
    with browser() as page:
        _open(page, HOME + "/search?q=" + quote(keyword, safe="") + "&src=typed_query")
        articles = page.locator(POSTS)
        _ready(page, articles)
        results = _cards(articles, 12)
        if not results and not page.locator(EMPTY).first.is_visible():
            raise BrowseError("X 网页没有返回可识别的帖子，请稍后再试。")
        return results


def read(value):
    url, post_id = post_url(value)
    with browser() as page:
        _open(page, url)
        # A thread can include parent posts, quotes, and replies. Match the
        # timestamp's own status link before returning the requested post.
        timestamp = page.locator(
            f"xpath=(.//time)[1]/ancestor::a[1][contains(concat(@href, '?'), '/status/{post_id}?')"
            f" or contains(concat(@href, '?'), '/status/{post_id}/?')]")
        articles = page.locator(POSTS).filter(has=timestamp)
        _ready(page, articles)
        results = _cards(articles, 12)
        for row in results:
            if post_url(row["url"])[1] == post_id:
                return [row]
        raise BrowseError("找不到这篇 X 帖子，可能已删除或无法访问。")


def digest(rows):
    lines = [f"X 帖子数: {len(rows)}"]
    for row in rows:
        lines.extend(("", row["url"], f'{row["author"]} {row["date"]}'.strip(),
                      row["text"] or "（页面没有可读取的正文）"))
    return "\n".join(lines)
