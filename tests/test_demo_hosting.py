import io
import json
import unittest

from api.index import app, demo
from canon.chart_demo import ChartDemo


class PublicDemoHostingTest(unittest.TestCase):
    def request(self, path, query="", method="GET"):
        response = {}

        def start_response(status, headers):
            response["status"] = int(status.split()[0])
            response["headers"] = dict(headers)

        body = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                             "wsgi.input": io.BytesIO(), "CONTENT_LENGTH": "0"}, start_response))
        return response, body

    def test_home_and_chart_page(self):
        response, _ = self.request("/")
        self.assertEqual(response["status"], 302)
        self.assertEqual(response["headers"]["Location"], "/chart-search")
        response, body = self.request("/chart-search")
        self.assertEqual(response["status"], 200)
        self.assertIn(b"Chart Search", body)

    def test_synthetic_chart_search_and_source_read(self):
        response, body = self.request("/v1/playground/charts")
        self.assertEqual(response["status"], 200)
        self.assertEqual(len(json.loads(body)["patients"]), 2)
        response, body = self.request("/v1/playground/charts/demo_maria_chen/search", "q=metformin")
        self.assertEqual(response["status"], 200)
        result = json.loads(body)["results"][0]
        response, body = self.request(f"/v1/playground/charts/demo_maria_chen/documents/{result['document_id']}/text")
        self.assertEqual(response["status"], 200)
        source = json.loads(body)["text"]
        self.assertEqual(result["excerpt"], source[result["start"]:result["end"]])

    def test_public_host_serves_only_synthetic_reads(self):
        for path, method in (("/v1/documents", "POST"), ("/v1/patients", "GET"), ("/mcp", "POST"),
                             ("/v1/playground/normalize", "POST"), ("/v1/playground/charts", "POST")):
            response, _ = self.request(path, method=method)
            self.assertEqual(response["status"], 404)
        self.assertEqual(demo.canon.list_patients(), [])

    def test_source_links_survive_a_new_serverless_instance(self):
        first = ChartDemo()
        second = ChartDemo()
        passage = first.canon.search("demo_maria_chen", "metformin")["results"][0]
        source = second.canon.document_text("demo_maria_chen", passage["document_id"])
        self.assertEqual(passage["excerpt"], source["text"][passage["start"]:passage["end"]])


if __name__ == "__main__":
    unittest.main()
