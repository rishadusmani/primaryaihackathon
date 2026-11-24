"""Vercel entry point for the public, read-only synthetic chart demo."""

from canon.api import App, Response


class DemoApp(App):
    def _route(self, method, path, query, headers, body):
        if method == "GET" and path == "/":
            return Response(302, "", headers={"Location": "/chart-search"})
        public_page = path in ("/chart-search", "/about", "/healthz")
        synthetic_chart = path == "/v1/playground/charts" or path.startswith("/v1/playground/charts/")
        if method == "GET" and (public_page or synthetic_chart):
            return super()._route(method, path, query, headers, body)
        return Response(404, {"error": {"code": "not_found", "message": "No demo endpoint found."}})


demo = DemoApp(api_keys=set())
app = demo.wsgi
