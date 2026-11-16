"""Cloudflare R2 提供方：S3 兼容协议，零新增依赖，标准库实现 AWS Signature V4。

- 前端直传：不支持。已实测 R2 未实现 S3 Presigned POST（返回 501
  NotImplemented），因此 ``supports_direct_upload=False``，上传策略接口会对
  R2 返回 ``direct=false``，前端自动回退为服务端中转上传（服务端再写入 R2）；
- 服务端读写：Authorization 头签名（SigV4，region=auto，service=s3），走
  https://{account_id}.r2.cloudflarestorage.com（R2 无内网/外网之分，全球同价免
  出口流量费）；
- 公网访问：Bucket 开启公共访问（r2.dev 子域或自定义域名），直接拼接公共 URL，
  不做查询串签名。

环境变量：
- OSS_PROVIDER=r2
- OSS_ACCOUNT_ID：Cloudflare 账户 ID
- OSS_BUCKET：R2 Bucket 名称
- OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET：R2 API Token（具备对象读写权限）
- OSS_PUBLIC_BASE：公共读域名（如 https://pub-xxxx.r2.dev 或自定义域名），
  必填——视频供应商（拾绘等）需要通过该公网 URL 拉取参考素材。
"""

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from services.oss.base import OSSProvider

_REGION = "auto"  # R2 的 SigV4 region 固定为 auto
_SERVICE = "s3"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class R2Provider(OSSProvider):
    name = "r2"

    def __init__(
        self,
        *,
        account_id: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        public_base: str = "",
        endpoint: str = "",
    ):
        self.account_id = account_id
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.public_base = (public_base or "").rstrip("/")
        self.endpoint = (
            endpoint or f"https://{account_id}.r2.cloudflarestorage.com"
        ).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(
            self.account_id
            and self.bucket
            and self.access_key_id
            and self.access_key_secret
        )

    # ---- 公网访问地址（Bucket 公共读，不签名） ----

    def public_url(self, key: str) -> str:
        if self.public_base:
            return f"{self.public_base}/{key}"
        # 未配置公共读域名时退化为 API 域名（不可公开读，仅保底不崩溃）；
        # 正常部署必须配置 OSS_PUBLIC_BASE，否则供应商无法拉取素材。
        return f"https://{self.bucket}.{self._host()}/{key}"

    def _normalize_aliyun_url(self, raw: str) -> str:
        """把指向本桶的完整 URL 降级为对象 key（去掉 query 与域名前缀）。

        方法名沿用基类钩子（历史命名），R2 同样生效。
        """
        if raw.startswith(("uploads/", "remake/")):
            return raw
        if raw.startswith(("http://", "https://")):
            for base in self._public_bases():
                if raw.startswith(base + "/"):
                    key, _ = _split_query(raw[len(base) + 1 :])
                    if key.startswith(("uploads/", "remake/")):
                        return key
        return raw

    def _public_bases(self) -> list[str]:
        bases: list[str] = []
        if self.public_base:
            bases.append(self.public_base)
        bases.append(f"https://{self.bucket}.{self._host()}")
        bases.append(f"{self.endpoint}/{self.bucket}")
        seen: set[str] = set()
        result: list[str] = []
        for item in bases:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def _host(self) -> str:
        return urlparse(self.endpoint).netloc

    # ---- 前端直传：R2 不支持 S3 Presigned POST（实测 501），
    # 不实现 sign_form_upload，由基类抛出 NotImplementedError；
    # supports_direct_upload=False 使策略接口返回 direct=false，前端回退服务端中转。

    # ---- 服务端读写（SigV4 Authorization 头，路径式寻址） ----

    def _object_url(self, key: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{quote(key, safe='/~')}"

    def _signed_headers(
        self, method: str, key: str, payload_hash: str
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = self._host()
        canonical_uri = f"/{self.bucket}/{quote(key, safe='/~')}"
        signed_header_names = "host;x-amz-content-sha256;x-amz-date"
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        canonical_request = (
            f"{method}\n{canonical_uri}\n\n{canonical_headers}\n"
            f"{signed_header_names}\n{payload_hash}"
        )
        scope = f"{date_stamp}/{_REGION}/{_SERVICE}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        signature = hmac.new(
            _signing_key(self.access_key_secret, date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key_id}/{scope}, "
                f"SignedHeaders={signed_header_names}, Signature={signature}"
            ),
        }

    async def exists(self, key: str) -> bool:
        """HEAD 探测对象是否已存在（内容寻址上传前的去重检查）。"""
        headers = self._signed_headers("HEAD", key, _EMPTY_SHA256)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.head(self._object_url(key), headers=headers)
            if response.status_code == 404:
                return False
            response.raise_for_status()
            return True

    async def get_bytes(self, key: str) -> bytes:
        headers = self._signed_headers("GET", key, _EMPTY_SHA256)
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(self._object_url(key), headers=headers)
            response.raise_for_status()
            return response.content

    async def download_to_file(self, key: str, destination: Path) -> None:
        """流式下载对象到文件，避免大媒体文件驻留内存。"""
        headers = self._signed_headers("GET", key, _EMPTY_SHA256)
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "GET", self._object_url(key), headers=headers
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as target:
                    async for chunk in response.aiter_bytes(
                        chunk_size=1024 * 1024
                    ):
                        await asyncio.to_thread(target.write, chunk)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str,
        *,
        cache_control: str | None = None,
    ) -> None:
        payload_hash = hashlib.sha256(data).hexdigest()
        headers = self._signed_headers("PUT", key, payload_hash)
        headers["Content-Type"] = content_type
        if cache_control:
            headers["Cache-Control"] = cache_control
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.put(
                self._object_url(key), content=data, headers=headers
            )
            response.raise_for_status()

    async def put_file(
        self,
        key: str,
        source: Path,
        content_type: str,
    ) -> None:
        """流式上传文件。SigV4 需要 payload 哈希，先流式算一遍再流式上传。"""
        digest = hashlib.sha256()

        def _hash_file() -> str:
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()

        payload_hash = await asyncio.to_thread(_hash_file)
        headers = self._signed_headers("PUT", key, payload_hash)
        headers["Content-Type"] = content_type

        async def chunks():
            with source.open("rb") as stream:
                while chunk := await asyncio.to_thread(
                    stream.read, 1024 * 1024
                ):
                    yield chunk

        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.put(
                self._object_url(key),
                content=chunks(),
                headers={
                    **headers,
                    "Content-Length": str(source.stat().st_size),
                },
            )
            response.raise_for_status()

    async def delete(self, key: str) -> None:
        """删除未绑定项目的暂存对象。"""
        headers = self._signed_headers("DELETE", key, _EMPTY_SHA256)
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.delete(
                self._object_url(key), headers=headers
            )
            response.raise_for_status()

    @asynccontextmanager
    async def open_download(self, key: str, *, byte_range: str | None = None):
        """流式读取对象，供作品库等接口按 Range 分段拉取。"""
        headers = self._signed_headers("GET", key, _EMPTY_SHA256)
        headers["Accept-Encoding"] = "identity"
        if byte_range:
            headers["Range"] = byte_range
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "GET", self._object_url(key), headers=headers
            ) as response:
                yield response


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str) -> bytes:
    """AWS SigV4 派生签名密钥：kDate → kRegion → kService → kSigning。"""
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, _REGION)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def _split_query(path_and_query: str) -> tuple[str, str]:
    if "?" not in path_and_query:
        return path_and_query, ""
    return tuple(path_and_query.split("?", 1))  # type: ignore[return-value]
