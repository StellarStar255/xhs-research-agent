import os
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from test_optimizations import IsolatedTest
from xhs_reader import agent, export, scraper, server, twitter
from xhs_reader.persistence import file_lock
from xhs_reader.tools import ToolRunner


class TwitterTests(IsolatedTest):
    def test_closed_login_window_has_a_retry_message(self):
        page = Mock()
        page.goto.side_effect = scraper.PlaywrightError("Target page has been closed")
        page.is_closed.return_value = True
        with patch.object(twitter, "browser", return_value=nullcontext(page)):
            with self.assertRaisesRegex(twitter.BrowseError, "登录窗口已关闭"):
                twitter.login()
        page.remove_listener.assert_called_once()

    def test_chrome_uses_its_own_profile_and_lock(self):
        context = Mock(pages=[object()])
        with file_lock(self.tmp / "browser.guard", timeout=0):
            with patch.object(scraper, "_browser_context", return_value=nullcontext(context)) as start:
                with twitter.browser(wait_s=0) as page:
                    self.assertIs(page, context.pages[0])
                    with self.assertRaises(TimeoutError), twitter.browser(wait_s=0):
                        pass
                start.assert_called_once_with(headless=False, profile_dir=self.tmp / "twitter-profile")

    def test_login_output_and_limits_are_independent(self):
        runner = ToolRunner()
        with patch.object(runner, "_cli", side_effect=[
                ("driver warning\nX_LOGIN_REQUIRED: 登录 X", True), ("SESSION xhs-session", False)]) as cli:
            result = runner.call("search_twitter", {"keyword": "OpenAI"})
            step = agent.tool_step("search_twitter", {"keyword": "OpenAI"}, "1")
            agent.apply_tool_output(step, result["text"], result["is_error"])
            self.assertEqual(step["status"], "login")
            self.assertTrue(runner.call("search_twitter", {"keyword": "再次搜索"})["is_error"])
            self.assertFalse(runner.call("search_xiaohongshu", {"keyword": "美食"})["is_error"])
            self.assertEqual(cli.call_count, 2)
            self.assertEqual((runner.x_calls, runner.searches), (1, 1))

        browser = Mock(returncode=0)
        browser.communicate.return_value = ("已登录 X\n", None)
        state = {"proc": browser, "state": "waiting", "message": ""}
        with patch.object(server, "_twitter_login", state), patch.object(server, "_status", {"value": None}):
            server._finish_twitter_login(browser)
            self.assertEqual(server.twitter_login_status()["state"], "done")
            self.assertIsNone(server._status["value"])

    def test_status_links_and_export(self):
        self.assertEqual(twitter.post_url("https://twitter.com/user/status/123/photo/1?s=20"),
                         ("https://x.com/user/status/123", "123"))
        for link in ("https://x.com.evil.test/user/status/123", "https://me@x.com/user/status/123",
                     "https://x.com/search?q=test", "https://x.com/user/status/123/../../home"):
            with self.subTest(link=link), self.assertRaises(twitter.BrowseError):
                twitter.post_url(link)
        self.assertEqual(len(export._sources([
            "[笔记](https://www.xiaohongshu.com/explore/123) [帖子](https://x.com/user/status/123) "
            "[重复](https://twitter.com/user/status/123)"])), 2)

    def test_selected_source_blocks_xiaohongshu_tools_in_x_turn(self):
        from xhs_reader.mcp_server import launch_spec

        runner = ToolRunner(source="twitter")
        with patch.object(runner, "_cli", return_value=(twitter.LIMITED, True)) as cli:
            self.assertTrue(runner.call("search_xiaohongshu", {"keyword": "问题"})["is_error"])
            cli.assert_not_called()
            result = runner.call("search_twitter", {"keyword": "问题"})
            step = agent.tool_step("search_twitter", {"keyword": "问题"}, "1")
            agent.apply_tool_output(step, result["text"], result["is_error"])
            self.assertEqual(step["status"], "limited")
            runner.call("search_twitter", {"keyword": "重试"})
            cli.assert_called_once()
        self.assertEqual(launch_spec("turn", "twitter")[2]["XHS_RESEARCH_SOURCE"], "twitter")
        self.assertNotIn("XHS_RESEARCH_SOURCE", launch_spec("turn")[2])

    def test_post_metadata_reaches_progress_step(self):
        step = agent.tool_step("search_twitter", {"keyword": "问题"}, "1")
        row = {"url": "https://twitter.com/user/status/123", "author": "作者", "date": "今天", "text": "<内容>"}
        agent.apply_tool_output(step, twitter.digest([row]))
        self.assertEqual(step["posts"], [{**row, "url": "https://x.com/user/status/123"}])

    def test_full_search_preserves_the_tool_output_header(self):
        articles = Mock()
        articles.evaluate_all.return_value = [
            {"url": f"https://x.com/user/status/{post_id}", "text": "文" * 6000,
             "author": "名" * 500, "date": "1" * 100}
            for post_id in range(1, 14)
        ]
        output = twitter.digest(twitter._cards(articles, 12))
        self.assertLess(len(output), 60000)
        self.assertTrue(output[-60000:].startswith("X 帖子数: 12\n"))

    @unittest.skipUnless(os.environ.get("XHS_BROWSER_TEST") == "1", "optional local Chrome check")
    def test_browser_login_search_and_exact_post(self):
        from playwright.sync_api import sync_playwright

        def article(post_id, body):
            return f"""<article data-testid="tweet">
              <div data-testid="User-Name">测试作者</div>
              <a href="/author/status/{post_id}"><time datetime="2026-11-24">今天</time></a>
              <div data-testid="tweetText">{body}</div>
            </article>"""

        login = '<script>setTimeout(() => document.body.innerHTML = ' \
                '\'<div data-testid="SideNav_AccountSwitcher_Button">账号</div>\', 150)</script>'
        posts = article(123, "目标正文") + article(456, "回复")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                def fixture(route):
                    url = route.request.url
                    if "/SearchTimeline" in url:
                        return route.fulfill(status=429, body="Too many requests")
                    if "background-limit" in url:
                        return route.fulfill(content_type="text/html", body=
                                             "<p>Loading</p><script>fetch('/i/api/graphql/test/SearchTimeline')</script>")
                    if "limited" in url:
                        return route.fulfill(status=429, body="Too many requests")
                    if "/status/123" in url:
                        quote = article(456, '引用 <a href="/author/status/123"><time>今天</time></a>')
                        original = article(123, "目标正文").replace('/status/123"', '/status/123?s=20"')
                        html = quote + f"<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend', `{original}`), 150)</script>"
                        return route.fulfill(content_type="text/html; charset=utf-8", body=html)
                    html = '<input autocomplete="username webauthn">' if "expired" in url else (
                        login if url.endswith("/home") else posts)
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)
                page.route("https://x.com/**", fixture)
                with patch.object(twitter, "browser", side_effect=lambda **_: nullcontext(page)):
                    twitter.login(timeout_s=3)
                    self.assertTrue(page.url.endswith("/home"))
                    self.assertEqual(len(twitter.search("中文关键词")), 2)
                    self.assertIn("中文关键词", page.locator("body").evaluate(
                        "() => new URL(location.href).searchParams.get('q')"))
                    result = twitter.read("https://twitter.com/author/status/123?s=20")
                    self.assertEqual([row["text"] for row in result], ["目标正文"])
                    with self.assertRaisesRegex(twitter.BrowseError, "X_LOGIN_REQUIRED"):
                        twitter.search("expired")
                    with patch.object(server, "_twitter_login", {"state": "done", "message": "已登录 X"}):
                        self.assertEqual(server.twitter_login_status()["state"], "expired")
                    with self.assertRaisesRegex(twitter.BrowseError, "X_LIMITED"):
                        twitter.search("limited")
                    with self.assertRaisesRegex(twitter.BrowseError, "X_LIMITED"):
                        twitter.search("background-limit")
            finally:
                browser.close()
