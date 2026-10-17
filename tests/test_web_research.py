"""公开网络边界与证据链；网络替身只验证协议，真实联网另留独立记录。"""

import importlib
from unittest.mock import Mock

import httpx
import pytest

from knowledgeops_agent.sources.service import SourceService


def test_synthetic_dns_is_resolved_publicly_without_allowing_literal_private_ip(monkeypatch):
    module = importlib.import_module("knowledgeops_agent.sources.web")
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("198.18.2.97", 443))],
    )
    fallback = Mock(return_value=["151.101.0.223"])
    monkeypatch.setattr(module, "resolve_public_dns", fallback)
    assert module.resolve_addresses("docs.python.org", 443) == ["151.101.0.223"]
    fallback.assert_called_once_with("docs.python.org")
    assert module.resolve_addresses("198.18.2.97", 443) == ["198.18.2.97"]
    assert fallback.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://169.254.169.254",
        "http://[::1]",
        "https://user:password@example.org",
        "http://2130706433",
        "http://10.0.0.1",
        "http://example.org:8000",
        "http://[::ffff:127.0.0.1]",
    ],
)
def test_private_or_non_public_targets_never_reach_transport(url):
    module = importlib.import_module("knowledgeops_agent.sources.web")
    called = Mock()
    reader = module.PublicWebReader(transport=httpx.MockTransport(called))
    with pytest.raises(ValueError):
        reader.fetch(url)
    called.assert_not_called()


def test_public_address_is_pinned_and_redirect_is_rechecked():
    module = importlib.import_module("knowledgeops_agent.sources.web")
    seen = []

    def transport(request):
        seen.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    reader = module.PublicWebReader(
        resolver=lambda host, port: ["93.184.215.14"] if host == "example.org" else [host],
        transport=httpx.MockTransport(transport),
    )
    with pytest.raises(ValueError, match="public"):
        reader.fetch("https://example.org/article")
    assert len(seen) == 1


def test_mixed_public_private_dns_and_oversized_response_are_rejected():
    module = importlib.import_module("knowledgeops_agent.sources.web")
    reader = module.PublicWebReader(resolver=lambda *_: ["93.184.215.14", "10.0.0.4"])
    with pytest.raises(ValueError, match="public"):
        reader.fetch("https://example.org")
    reader = module.PublicWebReader(
        resolver=lambda *_: ["93.184.215.14"],
        max_bytes=100,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="a" * 101)),
    )
    with pytest.raises(ValueError, match="size"):
        reader.fetch("https://example.org")


def test_search_discovery_is_not_evidence_and_page_cache_preserves_snapshot(
    business_sessions, tmp_path
):
    module = importlib.import_module("knowledgeops_agent.sources.web")
    sources = SourceService(business_sessions, tmp_path)
    html = (
        b"<html><head><title>Recovery guide</title></head><body><article><h1>Recovery</h1><p>"
        + (
            b"A committed operation must not be repeated on recovery. The durable receipt records its result. "
            * 5
        )
        + b"</p></article></body></html>"
    )
    fetches = []

    def response(request):
        fetches.append(request)
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    searcher = Mock(
        return_value=[
            {
                "title": "Recovery guide",
                "href": "https://example.org/guide",
                "body": "discovery only",
            }
        ]
    )
    service = module.WebResearchService(
        sources,
        reader=module.PublicWebReader(
            resolver=lambda *_: ["93.184.215.14"], transport=httpx.MockTransport(response)
        ),
        searcher=searcher,
    )
    discovered = service.search("durable recovery")
    assert discovered[0]["status"] == "not_read"
    assert "evidence_id" not in discovered[0] and sources.list_sources() == []
    first = service.read("https://example.org/guide", root_id="research-one")
    again = service.read("https://example.org/guide", root_id="research-two")
    assert len(fetches) == 1 and again["cached"] is True
    assert first["source"]["version_id"] == again["source"]["version_id"]
    assert first["evidence"]["root_id"] != again["evidence"]["root_id"]
    assert "committed operation" in first["evidence"]["text"]
    assert sources.original(first["source"]["version_id"])[0] == html


def test_empty_extraction_and_search_failure_are_not_successful_sources(
    business_sessions, tmp_path
):
    module = importlib.import_module("knowledgeops_agent.sources.web")
    sources = SourceService(business_sessions, tmp_path)
    reader = module.PublicWebReader(
        resolver=lambda *_: ["93.184.215.14"],
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text="<html><body></body></html>", headers={"content-type": "text/html"}
            )
        ),
    )
    service = module.WebResearchService(
        sources, reader=reader, searcher=Mock(side_effect=TimeoutError("private diagnostic"))
    )
    with pytest.raises(ValueError, match="search.*TimeoutError"):
        service.search("test")
    with pytest.raises(ValueError, match="extract"):
        service.read("https://example.org/empty", root_id="root")
    assert sources.list_sources() == []
