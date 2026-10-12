"""Contract tests for AstralNexus.runtime.subprocess_runtime and
AstralNexus.runtime.frontend_static.

subprocess_runtime: the one scrub definition (credential-shaped names,
DSH_* prefix with the Windows case-insensitivity rationale, PATH/locale
survival, the proxy child-environment overlay with deletion semantics),
the abstract service, the lookup error, and the control-channel
vocabulary.

frontend_static: serve_static's traversal fence (403), the index
authorization order (rejection answers before any bytes are read), miss
families (404 vs rethrow), the MIME table with octet-stream fallback, the
base-href splice, config validation, and the Flask fallback composition
over a real temporary dist (405 for non-GET/HEAD, decoded pathnames).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import pytest

from AstralNexus.runtime import frontend_static as fs
from AstralNexus.runtime import http_proxy
from AstralNexus.runtime import subprocess_runtime as sp


# ------------------------------------------------------- subprocess_runtime

class TestScrubbedParentEnv:
    @pytest.fixture()
    def env_patch(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "leak-me")
        monkeypatch.setenv("some_secret", "leak-me-too")
        monkeypatch.setenv("MyPassword", "leak-three")
        monkeypatch.setenv("AUTH_TOKEN", "leak-four")
        monkeypatch.setenv("DSH_HOME", "harness-identity")
        monkeypatch.setenv("dsh_lower", "windows-case-rationale")
        monkeypatch.setenv("PATHISH_NORMAL", "survives")
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("all_proxy", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # no proxy installed: the overlay must not invent names
        monkeypatch.setattr(http_proxy, "_active", None)
        return monkeypatch

    def test_credential_shapes_never_forwarded(self, env_patch) -> None:
        env = sp.scrubbed_parent_env()
        for name in ("DEEPSEEK_API_KEY", "some_secret", "MyPassword",
                     "AUTH_TOKEN"):
            assert name not in env

    def test_dsh_prefix_scrubbed_case_insensitively(self, env_patch) -> None:
        env = sp.scrubbed_parent_env()
        assert "DSH_HOME" not in env
        # a lowercase dsh_* parent entry would read back as $env:DSH_* in a
        # Windows child — the uppercase comparison scrubs it too
        assert "dsh_lower" not in env

    def test_ordinary_names_survive(self, env_patch) -> None:
        env = sp.scrubbed_parent_env()
        assert env["PATHISH_NORMAL"] == "survives"
        # PATH itself survives so child CLIs run normally
        path_key = next((k for k in env if k.upper() == "PATH"), None)
        assert path_key is not None

    def test_sensitive_pattern_is_the_upstream_regex(self) -> None:
        assert sp.SENSITIVE_ENV_PATTERN.pattern == "KEY|PASSWORD|SECRET|TOKEN"
        assert sp.SENSITIVE_ENV_PATTERN.flags & 2  # re.IGNORECASE

    def test_fresh_dict_per_call(self, env_patch) -> None:
        first = sp.scrubbed_parent_env()
        first["INJECTED"] = "x"
        assert "INJECTED" not in sp.scrubbed_parent_env()

    def test_proxy_overlay_publishes_and_deletes(self, env_patch,
                                                 monkeypatch) -> None:
        calls = []

        def fake_child_env() -> Dict[str, Optional[str]]:
            calls.append(1)
            return {"http_proxy": "http://resolved:3128",
                    "HTTP_PROXY": "http://resolved:3128",
                    "no_proxy": "localhost,127.0.0.1",
                    "NO_PROXY": "localhost,127.0.0.1",
                    "https_proxy": None, "HTTPS_PROXY": None,
                    "all_proxy": None, "ALL_PROXY": None,
                    "REQUESTS_CA_BUNDLE": None}
        monkeypatch.setattr(sp.http_proxy, "proxy_environment_for_child",
                            fake_child_env)
        env_patch.setenv("https_proxy", "user-value")
        env = sp.scrubbed_parent_env()
        assert calls == [1]
        assert env["http_proxy"] == "http://resolved:3128"
        assert env["no_proxy"] == "localhost,127.0.0.1"
        # a None slot DELETES — even a user value the parent had set
        assert "https_proxy" not in env
        assert "REQUESTS_CA_BUNDLE" not in env


class TestSubprocessContract:
    def test_abstract_runtime_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            sp.SubprocessRuntime()  # type: ignore[abstract]

    def test_service_key(self) -> None:
        assert sp.SubprocessRuntime.SERVICE_KEY == "subprocess"

    def test_executable_not_found_error(self) -> None:
        cause = FileNotFoundError("gone")
        error = sp.SubprocessExecutableNotFoundError("lookup failed", cause)
        assert error.name == "SubprocessExecutableNotFoundError"
        assert str(error) == "lookup failed"
        assert error.__cause__ is cause

    def test_control_vocabulary(self) -> None:
        assert sp.SUBPROCESS_CONTROL_FD == 7
        assert sp.SUBPROCESS_CONTROL_ENV == "DSH_SUBPROCESS_CONTROL"
        assert sp.DSH_ENV_PREFIX == "DSH_"


# --------------------------------------------------------- frontend_static

DIST_FIXTURES = {
    "index.html": "<!doctype html><html><head><title>x</title></head>"
                  "<body>root</body></html>",
    "app.js": "console.log(1)",
    "style.css": "body{}",
    "data.json": "{}",
    "logo.svg": "<svg/>",
    "manifest.webmanifest": "{}",
    "archive.gz": "binary-ish",
    "unknown.bin": "raw",
}


@pytest.fixture()
def dist(tmp_path):
    root = tmp_path / "dist"
    root.mkdir()
    for name, content in DIST_FIXTURES.items():
        (root / name).write_text(content, encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "page.html").write_text("<html><head></head></html>",
                                               encoding="utf-8")
    return root


class TestServeStatic:
    def serve(self, dist, pathname, authorized=True, rendered=None):
        index = str(dist / "index.html")
        calls: list = []

        def authorize():
            calls.append("authorize")
            if authorized:
                return None
            return 401, {"content-type": "text/plain"}, b"login required"

        def render():
            calls.append("render")
            return rendered if rendered is not None else "<html>rendered</html>"

        response = fs.serve_static(pathname, str(dist), index, authorize,
                                   render)
        return response, calls

    def test_dist_root_renders_the_index(self, dist) -> None:
        (status, headers, body), calls = self.serve(dist, "/")
        assert status == 200
        assert headers["content-type"] == fs.HTML_MIME
        assert body == b"<html>rendered</html>"
        assert calls == ["authorize", "render"]

    def test_explicit_index_path_renders(self, dist) -> None:
        (status, _, _), calls = self.serve(dist, "/index.html")
        assert status == 200
        assert calls == ["authorize", "render"]

    def test_authorization_answers_before_any_bytes(self, dist) -> None:
        (status, headers, body), calls = self.serve(dist, "/",
                                                    authorized=False)
        assert status == 401
        assert body == b"login required"
        # the rejection short-circuits: the index is never rendered
        assert calls == ["authorize"]

    def test_non_index_assets_stay_public(self, dist) -> None:
        (status, _, _), calls = self.serve(dist, "/app.js")
        assert status == 200
        assert calls == []  # neither authorize nor render ran

    @pytest.mark.parametrize("name,expected", [
        ("/app.js", "text/javascript; charset=utf-8"),
        ("/style.css", "text/css; charset=utf-8"),
        ("/data.json", "application/json"),
        ("/logo.svg", "image/svg+xml"),
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/archive.gz", "application/gzip"),
        ("/unknown.bin", "application/octet-stream"),
        ("/nested/page.html", "text/html; charset=utf-8"),
    ])
    def test_mime_table(self, dist, name: str, expected: str) -> None:
        (status, headers, body), _ = self.serve(dist, name)
        assert status == 200
        assert headers["content-type"] == expected

    def test_missing_path_is_404(self, dist) -> None:
        (status, _, _), _ = self.serve(dist, "/nope.js")
        assert status == 404

    def test_directory_target_is_a_miss_not_a_crash(self, dist) -> None:
        (status, _, _), _ = self.serve(dist, "/nested")
        assert status == 404

    @pytest.mark.parametrize("pathname", [
        "/../secret.txt", "/nested/../../secret.txt",
        "/..%2f".replace("%2f", "/") + "secret.txt",
    ])
    def test_traversal_is_403(self, dist, pathname: str) -> None:
        (status, _, _), _ = self.serve(dist, pathname)
        assert status == 403

    def test_traversal_check_allows_root_itself(self, dist) -> None:
        (status, _, _), _ = self.serve(dist, "")
        assert status == 200  # resolves to the dist root → index

    def test_genuine_permission_failure_rethrows(self, dist,
                                                 monkeypatch) -> None:
        real_isdir = os.path.isdir
        target = str(dist / "app.js")

        def fake_open(*args, **kwargs):
            raise PermissionError(13, "denied")
        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(fs.os.path, "isdir",
                            lambda p: False if p == target else real_isdir(p))
        with pytest.raises(PermissionError):
            fs.serve_static("/app.js", str(dist), str(dist / "index.html"),
                            lambda: None, lambda: "<html></html>")


class TestBaseHrefAndConfig:
    def test_splice_after_head_open(self) -> None:
        assert fs.splice_base_href("<html><head><title>t</title></head></html>") == \
            '<html><head><base href="./"><title>t</title></head></html>'

    def test_splice_with_head_attributes_and_case(self) -> None:
        assert fs.splice_base_href("<HEAD lang='x'><meta></HEAD>") == \
            "<HEAD lang='x'><base href=\"./\"><meta></HEAD>"

    def test_splice_only_first_head(self) -> None:
        result = fs.splice_base_href("<head></head><svg><head></head></svg>")
        assert result.count('<base href="./">') == 1

    def test_config_required(self) -> None:
        assert fs.validate_frontend_static_config(
            {"dist_index": "/x/index.html"}) == {"dist_index": "/x/index.html"}
        for bad in (None, {}, {"dist_index": ""}, {"dist_index": 5}):
            with pytest.raises(ValueError):
                fs.validate_frontend_static_config(bad)


class TestFlaskComposition:
    def make_app(self, dist, authorized=True):
        from flask import Flask
        app = Flask(__name__)
        state = {"authorized_calls": 0}

        def authorize():
            state["authorized_calls"] += 1
            if authorized:
                return None
            return 401, {}, b"login"

        fs.register_frontend_static_routes(app, str(dist / "index.html"),
                                           authorize)
        return app, state

    def test_get_and_head_and_methods(self, dist) -> None:
        app, _ = self.make_app(dist)
        client = app.test_client()
        response = client.get("/")
        assert response.status_code == 200
        assert b'<base href="./">' in response.data
        assert response.headers["content-type"] == fs.HTML_MIME
        head = client.head("/")
        assert head.status_code == 200
        post = client.post("/index.html")
        assert post.status_code == 405
        delete = client.delete("/app.js")
        assert delete.status_code == 405

    def test_asset_and_traversal_and_miss(self, dist) -> None:
        app, state = self.make_app(dist)
        client = app.test_client()
        response = client.get("/app.js")
        assert response.status_code == 200
        assert response.headers["content-type"] == \
            "text/javascript; charset=utf-8"
        assert state["authorized_calls"] == 0  # assets stay public
        assert client.get("/../outside.txt").status_code in (403, 404)
        assert client.get("/missing.css").status_code == 404
        assert client.get("/").status_code == 200
        assert state["authorized_calls"] == 1  # index passes the fence

    def test_unauthorized_index_answers_401(self, dist) -> None:
        app, _ = self.make_app(dist, authorized=False)
        client = app.test_client()
        response = client.get("/")
        assert response.status_code == 401
        assert response.data == b"login"

    def test_custom_render_injection(self, dist) -> None:
        from flask import Flask
        app = Flask(__name__)

        def render():
            return fs.splice_base_href("<html><head></head>"
                                       "<body>injected-row</body></html>")

        fs.register_frontend_static_routes(app, str(dist / "index.html"),
                                           lambda: None, render)
        response = app.test_client().get("/")
        assert b"injected-row" in response.data
        assert b'<base href="./">' in response.data
