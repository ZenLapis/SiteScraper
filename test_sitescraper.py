import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sitescraper_core import (
    CrawlOptions,
    PageExtractor,
    SiteCrawler,
    export_records,
    normalize_url,
)


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /blocked\n"
            content_type = "text/plain"
        elif self.path == "/":
            body = (
                b"<html><head><title>Home</title><meta name='description' content='Demo site'></head>"
                b"<body><h1>Welcome</h1><a href='/about'>About</a>"
                b"<a href='/blocked'>Blocked</a><p>hello@example.com</p></body></html>"
            )
            content_type = "text/html"
        elif self.path == "/about":
            body = b"<html><head><title>About</title></head><body><h1>About us</h1></body></html>"
            content_type = "text/html"
        else:
            body = b"<html><head><title>Should not load</title></head></html>"
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class SiteScrapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_normalize_url(self):
        self.assertEqual(normalize_url("Example.com#top"), "https://example.com/")
        self.assertEqual(normalize_url("/about", "https://example.com/start"),
                         "https://example.com/about")

    def test_page_extractor(self):
        parser = PageExtractor("https://example.com/")
        parser.feed(
            "<title> Useful page </title><meta name='description' content='A demo'>"
            "<h1>Main heading</h1><a href='/next'>Next</a> hello@example.com"
        )
        result = parser.result()
        self.assertEqual(result["title"], "Useful page")
        self.assertEqual(result["description"], "A demo")
        self.assertEqual(result["h1"], "Main heading")
        self.assertIn("hello@example.com", result["emails"])
        self.assertEqual(result["links"], ["https://example.com/next"])

    def test_crawler_follows_links_and_respects_robots(self):
        options = CrawlOptions(self.base_url, max_pages=10, max_depth=2, delay=0, timeout=5)
        rows = SiteCrawler(options).crawl()
        by_path = {row["url"].split(self.base_url, 1)[-1]: row for row in rows}
        self.assertIn("", by_path)
        self.assertIn("about", by_path)
        self.assertEqual(by_path["blocked"]["error"], "Blocked by robots.txt")

    def test_csv_export(self):
        with tempfile.TemporaryDirectory() as temp:
            output = export_records([{"url": "https://example.com", "title": "Demo"}],
                                    Path(temp) / "results.csv")
            self.assertTrue(output.is_file())
            self.assertIn("Demo", output.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
