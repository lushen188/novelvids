"""阿里云 OSS 提供方：零新增依赖，标准库实现 OSS 签名版本 4（Signature Version 4）。

- 前端直传：POST 表单策略（V4：policy base64 + HMAC-SHA256 签名密钥链）
- 服务端读写：OSS4-HMAC-SHA256 头签名，默认走内网 endpoint 读写省流量；
- 公网访问：Bucket 为公共读，直接拼接公共 URL，不做查询串签名。

V4 升级说明（本地二开，2026-11-24）：上游原版为 V1 头签名（``OSS AK:signature``）
与 SHA1 表单策略签名。阿里云新建 Bucket 自 2024/2025 起默认禁止 V1 签名
（403 ``V1 signature is forbidden``，且该限制不可解除），因此本地将该提供方
升级为 V4 签名，要点：

- 签名密钥链前缀为 ``aliyun_v4`` + AccessKeySecret（区别于 AWS 的 ``AWS4``）；
- scope 终止符为 ``aliyun_v4_request``，服务为 ``oss``；
- canonical headers 仅签 ``x-oss-*`` 与 ``content-type`` / ``content-md5``
  （host 不参与签名）；
- payload 固定为 ``UNSIGNED-PAYLOAD``；
- region 从 endpoint 自动推导（``oss-<region>.aliyuncs.com``），无新增环境变量。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from services.oss.base import OSSProvider

_MAX_DIRECT_UPLOAD = 5 * 1024 * 1024 * 1024  # 直传策略默认上限 5GB
_REGION_FALLBACK = "cn-hangzhou"


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _iso_expiration() -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=15)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _split_query(path_and_query: str) -> tuple[str, str]:
    """把 "path?query" 拆成 (path, query)；无 query 时返回 ("",)。"""
    if "?" not in path_and_query:
        return path_and_query, ""
    path, query = path_and_query.split("?", 1)
    return path, query


class AliyunProvider(OSSProvider):
    name = "aliyun"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str,
        internal_endpoint: str,
        public_base: str,
        access_key_id: str,
        access_key_secret: str,
    ):
        self.bucket = bucket
        self.endpoint = endpoint
        self.internal_endpoint = internal_endpoint or endpoint
        self.public_base = (public_base or "").rstrip("/")
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region = self._derive_region(self.endpoint or self.internal_endpoint)

    @staticmethod
    def _derive_region(endpoint: str) -> str:
        """从 endpoint 推导 region：oss-cn-guangzhou[.internal].aliyuncs.com → cn-guangzhou。"""
        host = re.sub(r"^https?://", "", endpoint or "").split("/")[0]
        first = host.split(".")[0]
        if first.startswith("oss-"):
            first = first[len("oss-"):]
        if first.endswith("-internal"):
            first = first[: -len("-internal")]
        return first or _REGION_FALLBACK

    @property
    def enabled(self) -> bool:
        return bool(
            self.bucket
            and self.endpoint
            and self.access_key_id
            and self.access_key_secret
        )

    @property
    def supports_direct_upload(self) -> bool:
        """阿里云 OSS 支持 POST 表单策略浏览器直传（本类为 V4 签名实现）。"""
        return True

    # ---- 公网访问地址（Bucket 为公共读，不签名） ----

    def public_url(self, key: str) -> str:
        base = self.public_base or f"https://{self.bucket}.{self.endpoint}"
        return f"{base}/{key}"

    def resolve_url(self, raw: str | None) -> str | None:
        """把持久化的媒体地址解析为可访问 URL（公共读，无需签名）。

        落库时只存 OSS 对象 key（以 ``uploads/`` 开头）或完整 URL：
        - 裸 key：拼接公共读域名；
        - 完整 URL / 外部地址 / 本地路径：原样返回。
        """
        if not raw:
            return raw
        if raw.startswith(("uploads/", "remake/")):
            return self.public_url(raw)
        return raw

    def _normalize_aliyun_url(self, raw: str) -> str:
        """把指向本桶的完整 URL 降级为对象 key（去掉 query 与域名前缀）。"""
        if raw.startswith(("uploads/", "remake/")):
            return raw
        if raw.startswith("http://") or raw.startswith("https://"):
            for base in self._public_bases():
                if raw.startswith(base + "/"):
                    key, _ = _split_query(raw[len(base) + 1 :])
                    if key.startswith(("uploads/", "remake/")):
                        return key
        return raw

    def _public_bases(self) -> list[str]:
        """本桶对外可能的公网基地址（用于识别完整 URL 是否指向本桶）。"""
        bases: list[str] = []
        if self.public_base:
            bases.append(self.public_base)
        bases.append(f"https://{self.bucket}.{self.endpoint}")
        bases.append(f"http://{self.bucket}.{self.endpoint}")
        # 去重并保持顺序
        seen: set[str] = set()
        result: list[str] = []
        for item in bases:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    # ---- V4 签名（服务端读写） ----

    def _signing_key(self, date_stamp: str) -> bytes:
        """V4 签名密钥链：aliyun_v4+SK → date → region → oss → aliyun_v4_request。"""
        key = f"aliyun_v4{self.access_key_secret}".encode("utf-8")
        key = _hmac_sha256(key, date_stamp)
        key = _hmac_sha256(key, self.region)
        key = _hmac_sha256(key, "oss")
        return _hmac_sha256(key, "aliyun_v4_request")

    def _v4_headers(self, method: str, key: str, content_type: str = "") -> dict[str, str]:
        """构造 V4 签名请求头（canonical headers 只签 x-oss-* 与 content-type）。"""
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        headers: dict[str, str] = {
            "x-oss-date": amz_date,
            "x-oss-content-sha256": "UNSIGNED-PAYLOAD",
        }
        if content_type:
            headers["Content-Type"] = content_type
        signed = {name.lower(): str(value) for name, value in headers.items()}
        canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in sorted(signed))
        signed_names = ";".join(sorted(signed))
        canonical_uri = f"/{self.bucket}/{quote(key, safe='/~')}"
        # 各段以 \n 分隔：method / uri / query(空) / headers / additional(空) / payload
        canonical_request = (
            f"{method}\n{canonical_uri}\n\n{canonical_headers}\n\nUNSIGNED-PAYLOAD"
        )
        scope = f"{date_stamp}/{self.region}/oss/aliyun_v4_request"
        string_to_sign = (
            f"OSS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            f"OSS4-HMAC-SHA256 Credential={self.access_key_id}/{scope}, "
            f"Signature={signature}"
        )
        return headers

    # ---- 前端直传：POST 表单策略（V4） ----

    def sign_form_upload(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        max_size: int = _MAX_DIRECT_UPLOAD,
    ) -> dict:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        scope = f"{date_stamp}/{self.region}/oss/aliyun_v4_request"
        credential = f"{self.access_key_id}/{scope}"
        policy = {
            "expiration": _iso_expiration(),
            "conditions": [
                {"bucket": self.bucket},
                {"key": key},
                {"Content-Type": content_type},
                {"x-oss-signature-version": "OSS4-HMAC-SHA256"},
                {"x-oss-credential": credential},
                {"x-oss-date": amz_date},
                ["content-length-range", 1, max_size],
            ],
        }
        policy_b64 = base64.b64encode(
            json.dumps(policy, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        signature = hmac.new(
            self._signing_key(date_stamp),
            policy_b64.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return {
            # 直传必须使用 Bucket 虚拟主机域名（路径式已不再支持，POST 到
            # https://{bucket}.{endpoint}），CNAME 域名不支持 POST 表单上传。
            "url": f"https://{self.bucket}.{self.endpoint}",
            "fields": {
                "key": key,
                "policy": policy_b64,
                "x-oss-signature-version": "OSS4-HMAC-SHA256",
                "x-oss-credential": credential,
                "x-oss-date": amz_date,
                "x-oss-signature": signature,
                "Content-Type": content_type,
            },
        }

    # ---- 服务端读写（内网 endpoint，V4 签名） ----

    def _internal_url(self, key: str) -> str:
        # https://{bucket}.{internal_endpoint}/{key}
        return f"https://{self.bucket}.{self.internal_endpoint}/{quote(key, safe='/~')}"

    async def get_bytes(self, key: str) -> bytes:
        headers = self._v4_headers("GET", key)
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(
                self._internal_url(key), headers=headers
            )
            response.raise_for_status()
            return response.content

    @asynccontextmanager
    async def open_download(self, key: str, *, byte_range: str | None = None):
        headers = self._v4_headers("GET", key)
        headers["Accept-Encoding"] = "identity"
        if byte_range:
            headers["Range"] = byte_range
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("GET", self._internal_url(key), headers=headers) as response:
                yield response

    async def download_to_file(self, key: str, destination: Path) -> None:
        """使用 OSS 内网 endpoint 流式下载对象。"""
        async with self.open_download(key) as response:
            response.raise_for_status()
            with destination.open("wb") as target:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    await asyncio.to_thread(target.write, chunk)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str,
        *,
        cache_control: str | None = None,
    ) -> None:
        headers = self._v4_headers("PUT", key, content_type)
        if cache_control:
            headers["Cache-Control"] = cache_control
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.put(
                self._internal_url(key),
                content=data,
                headers=headers,
            )
            response.raise_for_status()

    async def put_file(
        self,
        key: str,
        source: Path,
        content_type: str,
    ) -> None:
        """使用 OSS 内网 endpoint 流式上传文件。"""
        headers = self._v4_headers("PUT", key, content_type)
        headers["Content-Length"] = str(source.stat().st_size)

        async def chunks():
            with source.open("rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
                    yield chunk

        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.put(
                self._internal_url(key),
                content=chunks(),
                headers=headers,
            )
            response.raise_for_status()

    async def delete(self, key: str) -> None:
        """删除未绑定项目的暂存对象。"""
        headers = self._v4_headers("DELETE", key)
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.delete(
                self._internal_url(key), headers=headers
            )
            response.raise_for_status()
