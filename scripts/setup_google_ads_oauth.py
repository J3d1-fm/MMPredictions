#!/usr/bin/env python3
"""Create ~/.google-ads.yaml from a Google Ads OAuth desktop client."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import stat
import socketserver
import urllib.parse

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPE = "https://www.googleapis.com/auth/adwords"
DEFAULT_CLIENT = pathlib.Path(
    os.environ.get(
        "GOOGLE_ADS_OAUTH_CLIENT",
        str(pathlib.Path.home() / "google-ads-oauth-client.json"),
    )
)
DEFAULT_OUTPUT = pathlib.Path.home() / ".google-ads.yaml"
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth-client", default=str(DEFAULT_CLIENT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--login-customer-id", required=True)
    parser.add_argument(
        "--developer-token",
        default=os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"),
        help="Google Ads developer token. Prefer GOOGLE_ADS_DEVELOPER_TOKEN env var.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.developer_token:
        raise SystemExit("GOOGLE_ADS_DEVELOPER_TOKEN or --developer-token is required")

    client_path = pathlib.Path(args.oauth_client)
    data = json.loads(client_path.read_text(encoding="utf-8"))
    installed = data["installed"]

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes=[SCOPE])

    class OneShotServer(socketserver.TCPServer):
        allow_reuse_address = True

    callback_error: list[BaseException] = []
    with OneShotServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler) as server:
        port = server.server_address[1]
        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, _state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
        )

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
                try:
                    full_url = f"http://localhost:{port}{self.path}"
                    parsed = urllib.parse.urlparse(full_url)
                    if urllib.parse.parse_qs(parsed.query).get("error"):
                        raise RuntimeError(parsed.query)
                    flow.fetch_token(authorization_response=full_url)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(
                        b"Google Ads OAuth is complete. You can close this tab."
                    )
                except BaseException as exc:  # noqa: BLE001
                    callback_error.append(exc)
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(exc).encode("utf-8", errors="replace"))

            def log_message(self, _format: str, *args: object) -> None:
                return

        server.RequestHandlerClass = CallbackHandler
        print("Open this URL in Safari and authorize Google Ads access:", flush=True)
        print(auth_url, flush=True)
        server.handle_request()

    if callback_error:
        raise callback_error[0]
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise SystemExit("OAuth completed but no refresh_token was returned")

    output = pathlib.Path(args.output).expanduser()
    content = "\n".join(
        [
            f'developer_token: "{args.developer_token}"',
            f'client_id: "{installed["client_id"]}"',
            f'client_secret: "{installed["client_secret"]}"',
            f'refresh_token: "{credentials.refresh_token}"',
            f'login_customer_id: "{args.login_customer_id}"',
            "use_proto_plus: true",
            "",
        ]
    )
    output.write_text(content, encoding="utf-8")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
