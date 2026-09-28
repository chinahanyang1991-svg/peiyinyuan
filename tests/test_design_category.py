"""VA-4 语音设计 category 透传的单元验证（不加载引擎、不写生产库）。"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import modules.voice_manager.voice_designer as vd_mod
from modules.voice_manager.voice_designer import VoiceDesigner


class _FakeEngine:
    supports_design = True
    calls = []

    def design_voice(self, description, voice_id=None):
        self.calls.append((voice_id, description))
        return voice_id


def test_design_passes_category(monkeypatch):
    fake = _FakeEngine()
    monkeypatch.setattr(vd_mod.engine_registry, "get_loaded", lambda name: fake)
    created = {}

    def fake_create(**kw):
        created.update(kw)
        return types.SimpleNamespace(id="vid-1", name=kw["name"])

    monkeypatch.setattr(vd_mod.voice_store, "create", fake_create)

    d = VoiceDesigner()
    out = d.design("25岁知性女声", name="x", category="anime")
    assert out["voice_id"] == "vid-1"
    assert created["category"] == "anime"
    assert created["source"] == "designed" and created["engine"] == "voxcpm"
    assert fake.calls and fake.calls[0][0] == "vid-1"


def test_design_without_category_keeps_default(monkeypatch):
    fake = _FakeEngine()
    monkeypatch.setattr(vd_mod.engine_registry, "get_loaded", lambda name: fake)
    created = {}
    monkeypatch.setattr(vd_mod.voice_store, "create",
                        lambda **kw: created.update(kw) or types.SimpleNamespace(id="v2", name="n"))

    d = VoiceDesigner()
    d.design("成年男声", name="y")
    assert "category" not in created  # 不写该字段，落到模型默认 human
