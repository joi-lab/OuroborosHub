from __future__ import annotations

from PIL import Image

from skills.anime_studio.api_client import OpenRouterClient, VLM_MAX_IMAGES


def test_parse_json_response_accepts_fenced_and_wrapped_objects(tmp_path):
    client = OpenRouterClient("test-key", tmp_path)

    assert client.parse_json_response('```json\n{"ok": true}\n```') == {"ok": True}
    assert client.parse_json_response('prefix {"ok": {"nested": true}} suffix') == {
        "ok": {"nested": True},
    }
    assert client.parse_json_response('[{"ok": true}]') == {"ok": True}


def test_vlm_image_parts_compresses_and_limits_images(tmp_path):
    client = OpenRouterClient("test-key", tmp_path)
    paths = []
    for idx in range(VLM_MAX_IMAGES + 2):
        path = tmp_path / f"frame_{idx}.png"
        Image.new("RGB", (1536, 864), (idx * 20 % 255, 80, 160)).save(path)
        paths.append(str(path))

    parts = client._vlm_image_parts(paths)

    assert len(parts) == VLM_MAX_IMAGES
    assert all(part["type"] == "image_url" for part in parts)
    assert all(
        part["image_url"]["url"].startswith("data:image/jpeg;base64,")
        for part in parts
    )
