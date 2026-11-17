import httpx
import pytest
import os
import subprocess
import sys

from config import settings
from models.config import AiModelConfig, StudioPlatformConnection
from services import studio_platform


@pytest.mark.parametrize(("configured", "expected"), [
    ("", None),
    (" https://platform.example/ ", "https://platform.example/v1"),
])
def test_studio_platform_default_and_deployment_override(configured, expected):
    subprocess.run([sys.executable, "-c",
        "import sys; from config import settings; from services.studio_platform import platform_base_url; "
        "url = platform_base_url(); assert url == (sys.argv[1] or settings.STUDIO_PLATFORM_BASE_URL); "
        "assert url.startswith('https://') and url.endswith('/v1')", expected or ""],
        env={**os.environ, "STUDIO_PLATFORM_BASE_URL": configured}, check=True)


@pytest.mark.asyncio
async def test_sync_includes_enhancement_and_speech_without_provider_options(monkeypatch):
    monkeypatch.setattr(settings, "STUDIO_PLATFORM_BASE_URL", "https://platform.example")
    await StudioPlatformConnection.all().delete()
    original_client = httpx.AsyncClient
    catalog = {"data": [
        {"id": "nv-801", "name": "增强", "task_types": [8],
         "pricing": {"type": "media_processing", "billing_unit": "minute", "tiers": [{"short_side_max": 1080, "fps_max": 30, "price": 1}]},
         "processing_options": {"output_resolutions": ["1080p", "4k"], "queue_id": "private-queue", "media_base_url": "https://private.example", "api_key": "secret"}},
        {"id": "nv-901", "name": "语音识别", "task_types": [9],
         "pricing": {"type": "asr", "price_per_minute": 0.02}},
    ]}
    monkeypatch.setattr(studio_platform.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=catalog))))
    assert (await studio_platform.sync_platform_models("nv_local"))["synced"] == 2
    enhancement = await AiModelConfig.get(scope=studio_platform.SYNC_SCOPE, model="nv-801")
    speech = await AiModelConfig.get(scope=studio_platform.SYNC_SCOPE, model="nv-901")
    assert enhancement.task_types == [8] and speech.task_types == [9]
    assert enhancement.processing_options["output_resolutions"] == ["1080p", "4k"]
    assert not {"queue_id", "media_base_url", "api_key"}.intersection(enhancement.processing_options)
    assert speech.pricing["price_per_minute"] == 0.02
    assert speech.base_url == "https://platform.example/v1" and speech.api_key == "nv_local"
    catalog["data"][0]["processing_options"]["output_resolutions"] = ["720p"]
    await studio_platform.sync_platform_models()
    await enhancement.refresh_from_db()
    assert enhancement.processing_options["output_resolutions"] == ["720p"]


@pytest.mark.asyncio
async def test_sync_deletes_disabled_models_and_unused_presets_but_keeps_custom_models(monkeypatch):
    from services.model_config_seed import DEFAULT_MODELS, ensure_model_config_seed_data
    monkeypatch.setattr(settings, "STUDIO_PLATFORM_BASE_URL", "https://platform.example")
    monkeypatch.setattr(settings, "STUDIO_PLATFORM_API_KEY", "")
    await StudioPlatformConnection.all().delete()
    await AiModelConfig.all().delete()
    preset = await AiModelConfig.create(**DEFAULT_MODELS[0], is_active=False)
    custom = await AiModelConfig.create(name="我的未启用模型", task_type=1, model="custom", base_url="https://custom.example", api_key="", is_active=False)
    original_client = httpx.AsyncClient
    catalog = {"data": [{"id": "nv-501", "name": "模型", "task_types": [1], "is_active": False}]}
    monkeypatch.setattr(studio_platform.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=catalog))))
    assert (await studio_platform.sync_platform_models("nv_local"))["synced"] == 0
    assert not await AiModelConfig.filter(id=preset.id).exists()
    assert await AiModelConfig.filter(id=custom.id).exists()
    assert await ensure_model_config_seed_data() == 0
    catalog["data"][0]["is_active"] = True
    assert (await studio_platform.sync_platform_models())["synced"] == 1
    catalog["data"][0]["is_active"] = False
    assert (await studio_platform.sync_platform_models())["synced"] == 0
    assert not await AiModelConfig.filter(scope=studio_platform.SYNC_SCOPE).exists()
    assert await AiModelConfig.filter(id=custom.id).exists()


@pytest.mark.asyncio
async def test_seedream_catalog_preserves_each_channels_local_tariff(monkeypatch):
    tariff = {"type": "image", "currency": "CNY", "billing_unit": "pixel_tier",
              "prices": {"low": 0.13, "high_capped": 0.23}, "discount": 0.5,
              "input_image": {"first_free": 0, "price_per_image": .05}}
    legacy = {"type": "image", "currency": "CNY", "prices": {"2K": .6}}
    pro = {**tariff, "prices": {"single_low": .3, "single_high": .6, "layer_low": .15, "layer_high": .3}}
    entries = [{"id": f"nv-{i}", "name": "Seedream", "task_types": [2],
                "image_model_type": kind, "pricing": price}
               for i, (kind, price) in enumerate([("seedream_5_5", tariff), ("seedream_5_pro", legacy),
                                                   ("seedream_5_pro", pro)], 1)]
    client = httpx.AsyncClient
    monkeypatch.setattr(studio_platform.httpx, "AsyncClient", lambda **kw: client(
        **kw, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": entries}))))
    await studio_platform.sync_platform_models("nv_test")
    rows = await AiModelConfig.filter(scope=studio_platform.SYNC_SCOPE).order_by("model")
    assert [row.pricing for row in rows] == [tariff, legacy, pro]
    entries[1]["pricing"] = pro
    assert await studio_platform.platform_image_tariff(rows[1].id) == pro
    await studio_platform.sync_platform_models()
    await rows[1].refresh_from_db()
    assert rows[1].pricing == legacy


@pytest.mark.asyncio
async def test_unknown_catalog_types_skip_one_channel_and_preserve_local_prices(monkeypatch):
    entries = [{"id":"nv-31", "name":"Image", "task_types":[2], "image_model_type":"seedream_5_5"},
               {"id":"nv-32", "name":"Video", "task_types":[4], "video_model_type":"future_video"}]
    client = httpx.AsyncClient
    monkeypatch.setattr(studio_platform.httpx, "AsyncClient", lambda **kw: client(**kw,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data":entries}))))
    result = await studio_platform.sync_platform_models("nv_test")
    assert result["synced"] == 1 and result["unsupported"] == {"nv-32":"future_video"}
    row = await AiModelConfig.get(model="nv-31")
    await AiModelConfig.filter(id=row.id).update(pricing={"type":"image","prices":{"low":.4}})
    entries[0]["image_model_type"] = "future_image"
    result = await studio_platform.sync_platform_models()
    assert result["synced"] == 0
    await row.refresh_from_db()
    assert not row.is_active and row.pricing["prices"]["low"] == .4
    entries[0]["image_model_type"] = "seedream_5_5"
    await studio_platform.sync_platform_models()
    await row.refresh_from_db()
    assert row.is_active and row.pricing["prices"]["low"] == .4
