"""公开网页发现、受控抓取与来源快照。

搜索结果只用于发现 URL；实际读取后才产生可引用证据。DNS 结果验证后直接
连接该 IP，并保留 Host/TLS 域名校验，避免验证与连接之间再次解析被替换。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urljoin

import httpx
import trafilatura
from ddgs import DDGS

from knowledgeops_agent.sources.search_config import (
    SearchTimeRange,
    WebSearchConfig,
    load_web_search_config,
)
from knowledgeops_agent.sources.service import SourceService


def public_url(value: str) -> httpx.URL:
    """仅允许标准端口的公开 HTTP(S) 页面，不接受凭据、文件路径或本地协议。"""
    if len(value) > 4096:
        raise ValueError("web URL is too long")
    try:
        url = httpx.URL(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.port not in {None, 80, 443}
        ):
            raise ValueError("only public HTTP(S) URLs on ports 80/443 are allowed")
        return url.copy_with(fragment=None)
    except httpx.InvalidURL:
        raise ValueError("invalid public web URL") from None


def resolve_addresses(host: str, port: int) -> list[str]:
    """优先系统 DNS；代理返回合成地址时取得真实公网解析，显式 IP 不走回退。"""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    addresses = list(
        dict.fromkeys(
            str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
    )
    synthetic = [ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48")]
    if addresses and all(
        any(ipaddress.ip_address(ip) in block for block in synthetic) for ip in addresses
    ):
        if "." not in host or host.endswith((".local", ".internal", ".localhost")):
            raise ValueError("web target must be a public domain")
        return resolve_public_dns(host)
    return addresses


def resolve_public_dns(host: str) -> list[str]:
    """通过固定 Cloudflare DoH 解析，不使用用户可指定的 DNS 服务。

    只发送公开域名和记录类型，不发送页面路径、Cookie 或文件内容。固定 IP
    引导避免再次依赖合成 DNS；TLS 仍验证 cloudflare-dns.com，结果仍需公网校验。
    """
    addresses: list[str] = []
    with httpx.Client(trust_env=False, timeout=8) as client:
        for record_type in ("A", "AAAA"):
            response = client.get(
                "https://1.1.1.1/dns-query",
                params={"name": host, "type": record_type},
                headers={"Host": "cloudflare-dns.com", "Accept": "application/dns-json"},
                extensions={"sni_hostname": "cloudflare-dns.com"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("Status") != 0:
                raise ValueError("public DNS resolution failed")
            addresses.extend(
                str(row["data"]) for row in payload.get("Answer", []) if row.get("type") in {1, 28}
            )
    return list(dict.fromkeys(addresses))


@dataclass(frozen=True)
class WebSnapshot:
    url: str
    data: bytes
    mime_type: str


class PublicWebReader:
    """有大小/超时/重定向上限的 GET 客户端；可注入传输以验证安全边界。

    每个跳转重新校验全部 DNS 地址，拒绝任一非公网地址；实际连接固定到
    已校验地址。禁用环境代理与自动重定向，不继承用户站点 Cookie 或凭据。
    """

    def __init__(
        self, *, resolver=resolve_addresses, transport=None, max_bytes: int = 2 * 1024 * 1024
    ):
        self.resolver = resolver
        self.transport = transport
        self.max_bytes = max_bytes

    def fetch(self, value: str) -> WebSnapshot:
        current = public_url(value)
        deadline = monotonic() + 40
        try:
            # 每次读取独立会话；不同来源不能继承其他页面的 Set-Cookie。
            with httpx.Client(
                trust_env=False, follow_redirects=False, transport=self.transport, timeout=12
            ) as client:
                for _ in range(5):
                    addresses = self.resolver(
                        current.host, current.port or (443 if current.scheme == "https" else 80)
                    )
                    if not addresses or any(
                        not ipaddress.ip_address(ip).is_global for ip in addresses
                    ):
                        raise ValueError("web target must resolve exclusively to public addresses")
                    # 优先 IPv4；保留 IPv6-only 站点能力。TLS 验证使用原域名，不能关闭 verify。
                    address = sorted(addresses, key=lambda ip: ":" in ip)[0]
                    target = current.copy_with(host=address)
                    client.cookies.clear()
                    with client.stream(
                        "GET",
                        target,
                        headers={
                            "Host": current.netloc.decode("ascii"),
                            "User-Agent": "KnowledgeOps/0.1 (public research)",
                            "Accept-Encoding": "identity",
                        },
                        extensions={"sni_hostname": current.host},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("redirect has no location")
                            current = public_url(urljoin(str(current), location))
                            continue
                        if response.status_code != 200:
                            raise ValueError(f"web read failed with HTTP {response.status_code}")
                        mime = (
                            response.headers.get("content-type", "text/plain")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if mime not in {
                            "text/html",
                            "application/xhtml+xml",
                            "text/plain",
                            "text/markdown",
                        }:
                            raise ValueError(
                                "unsupported web content type; upload PDF files through materials"
                            )
                        content = bytearray()
                        for piece in response.iter_bytes(chunk_size=16384):
                            if monotonic() > deadline:
                                raise ValueError("web read time limit exceeded")
                            if len(content) + len(piece) > self.max_bytes:
                                raise ValueError("web response size limit exceeded")
                            content.extend(piece)
                        return WebSnapshot(str(current), bytes(content), mime)
                raise ValueError("web redirect limit exceeded")
        except (httpx.HTTPError, OSError) as exc:
            # 不把底层请求头或任意代理配置放进工具反馈。
            raise ValueError(f"web read failed ({type(exc).__name__})") from None


class WebResearchService:
    """存储独立于运行；缓存可以复用，读取证据仍归属本次根任务。"""

    def __init__(
        self,
        sources: SourceService,
        *,
        reader=None,
        searcher=None,
        search_config: WebSearchConfig | None = None,
    ):
        self.sources = sources
        self.reader = reader or PublicWebReader()
        self.search_config = (
            search_config if search_config is not None else load_web_search_config()
        )
        self.searcher = searcher or self._search

    def _search(
        self, query: str, count: int, *, time_range: SearchTimeRange | None = None
    ) -> list[dict]:
        """仍直接调用DDGS SDK；MCP不是搜索配置或质量提升的前提。"""
        kwargs = self.search_config.search_kwargs(query, count=count, time_range=time_range)
        return DDGS(timeout=self.search_config.timeout_seconds).text(query, **kwargs)

    def search(
        self, query: str, *, count: int | None = None, time_range: SearchTimeRange | None = None
    ) -> list[dict]:
        """返回发现摘要，不创建 Source/Evidence，也不把摘要宣称为页面正文。"""
        if not query.strip() or len(query) > 500:
            raise ValueError("search query must contain 1-500 characters")
        # 参数校验在网络异常包装之前，模型能收到可据以改正的具体反馈。
        kwargs = self.search_config.search_kwargs(query, count=count, time_range=time_range)
        count = kwargs["max_results"]
        try:
            results = self.searcher(query, count, time_range=time_range)
        except Exception as exc:
            raise ValueError(f"web search failed ({type(exc).__name__})") from None
        valid = []
        for row in results[:count]:
            try:
                url = str(public_url(row.get("href") or row.get("url") or ""))
            except ValueError:
                continue
            valid.append(
                {
                    "title": str(row.get("title", ""))[:500],
                    "url": url,
                    "snippet": str(row.get("body", ""))[:700],
                    "status": "not_read",
                }
            )
        return valid

    def import_url(self, url: str, *, refresh: bool = False) -> tuple[dict, bool]:
        """取得正文快照，但此时模型还没看到正文，因此尚不登记已读证据。"""
        url = str(public_url(url))
        cached = None if refresh else self.sources.cached_web(url)
        if cached:
            return cached, True
        snapshot = self.reader.fetch(url)
        if snapshot.mime_type in {"text/plain", "text/markdown"}:
            title, body = snapshot.url, snapshot.data.decode("utf-8", errors="replace")
        else:
            document = trafilatura.bare_extraction(
                snapshot.data,
                url=snapshot.url,
                include_comments=False,
                include_links=False,
                with_metadata=True,
            )
            if document is None or isinstance(document, dict) or not document.text:
                raise ValueError("could not extract readable page content")
            title, body = document.title or snapshot.url, document.text
        if len(body.strip()) < 60:
            raise ValueError("could not extract enough readable page content")
        source = self.sources.import_text(
            title[:500],
            body,
            kind="web",
            origin=snapshot.url,
            raw_bytes=snapshot.data,
            mime_type=snapshot.mime_type,
            parser="trafilatura-v2" if "html" in snapshot.mime_type else "text-v1",
        )
        return source, False

    def read(self, url: str, *, root_id: str, refresh: bool = False) -> dict:
        """把首段实际正文交给模型；后续片段必须再读，不能将下载等同于全文已读。"""
        source, cached = self.import_url(url, refresh=refresh)
        evidence = self.sources.read_segment(source["segments"][0]["id"], root_id=root_id)
        return {
            "source": source,
            "cached": cached,
            "evidence": evidence,
            "remaining_segments": source["segments"][1:],
        }
