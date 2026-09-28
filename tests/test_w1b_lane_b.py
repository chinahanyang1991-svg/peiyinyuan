"""波次1 泳道B (US-B1..US-B5) 单元测试 —— 全程 mock 网络，不连真 :9880 服务。

- US-B1: text_lang 由待合成文本自测语种；静态断言 payload.text_lang 不得等于
         prompt_lang（除非句内确实只有该语种字符）。
- US-B2: SplitPolicy 注入，默认与现状逐字等价；gptsovits 档 max45/min6/tail/upper500。
- US-B3: 空 prompt_text -> 合成前 prompt-free 告警进 task_log（非硬错）。
- US-B4: payload 白名单校验（拼错键/越界报错）+ 一切降级/互斥写 task_log。
- US-B5: 音色分组纯函数 index 序列等价 + 换组次数下降；warm_reference 只 warn 不抛。
"""
import io
import os
import sys
import types

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import modules.tts_engine.engines.gptsovits_adapter as ga
from modules.text_processor.sentence_splitter import (
    SentenceSplitter, SplitPolicy, DEFAULT_SPLIT_POLICY, GPTSOVITS_SPLIT_POLICY)
from modules.workflow.conversion_workflow import (
    group_gptsovits_execution_order, count_gsv_reference_switches,
    stricter_split_policy)


def _wav_bytes(sr=32000, seconds=1.0, level=0.12):
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    y = (level * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, y, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


@pytest.fixture
def net(monkeypatch):
    """requests.get/post 打桩：POST /tts 回合法 wav，GET 回 200；记录所有 payload/calls。"""
    import requests
    posts, gets = [], []

    def _post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json, "timeout": timeout})
        return _Resp(200, _wav_bytes())

    def _get(url, params=None, timeout=None):
        gets.append({"url": url, "params": params, "timeout": timeout})
        return _Resp(200)

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(requests, "get", _get)
    return posts, gets


def _adapter_with_ref(monkeypatch, prompt_text="参考转写", prompt_lang="zh"):
    eng = ga.GPTSoVITSAdapter(base_url="http://x:9880")
    eng._loaded = True
    monkeypatch.setattr(
        ga.GPTSoVITSAdapter, "_ref_for_voice",
        lambda self, vid: ("/abs/ref.wav", prompt_text, prompt_lang))
    return eng


# ============================================================ US-B1
class TestB1TextLang:
    @pytest.mark.parametrize("text,expected", [
        ("今天天气很好，我们出发。", "zh"),          # 纯 CJK
        ("This is absolutely wonderful.", "en"),      # 纯拉丁
        ("“OK，我们出发。”", "auto"),                 # 中英混排 -> auto
        ("你胸前这枚玉佩，是 Dr. Lee 留给我的。", "auto"),
        ("去做个B超。", "zh"),                        # 单字母不判为拉丁实词
        ("Hello 世界。", "auto"),
    ])
    def test_detect_table(self, text, expected):
        assert ga._detect_text_lang(text) == expected

    def test_payload_text_lang_not_from_prompt_lang(self, net, monkeypatch):
        # 中文参考音频 + 中英混排文本：text_lang 必须是 auto，不得被 prompt_lang(zh) 带偏
        posts, _ = net
        eng = _adapter_with_ref(monkeypatch, prompt_text="参考", prompt_lang="zh")
        eng.synthesize("“OK，我们出发。”", voice_id="v1")
        payload = posts[0]["json"]
        assert payload["text_lang"] == "auto"
        assert payload["prompt_lang"] == "zh"
        # 静态断言（SPEC/M3 硬锚）：混排句的 text_lang 恒 != prompt_lang
        assert payload["text_lang"] != payload["prompt_lang"]

    def test_static_invariant_pure_lang_allowed_equal(self, net, monkeypatch):
        # 纯中文句 + zh 参考：text_lang==prompt_lang 是允许的（句内确实只有该语种）
        posts, _ = net
        eng = _adapter_with_ref(monkeypatch, prompt_text="参考", prompt_lang="zh")
        eng.synthesize("这是一段纯中文。", voice_id="v1")
        payload = posts[0]["json"]
        assert payload["text_lang"] == "zh"
        # 不变式：text_lang 与 prompt_lang 相等 仅当句内无其它语种实词
        assert not ga._LATIN_WORD_RE.search("这是一段纯中文。")

    def test_language_table_has_auto(self):
        assert "auto" in ga.SUPPORTED_TEXT_LANGS
        assert {"all_zh", "all_en"} <= ga.SUPPORTED_TEXT_LANGS


# ============================================================ US-B2
class TestB2SplitPolicy:
    def test_default_policy_equals_legacy_constants(self):
        sp = SentenceSplitter()
        assert sp.MAX_SENTENCE_LENGTH == 150
        assert sp.MIN_SENTENCE_LENGTH == 2
        assert sp.MAX_EN_SENTENCE == 300
        assert (DEFAULT_SPLIT_POLICY.max_len, DEFAULT_SPLIT_POLICY.min_len,
                DEFAULT_SPLIT_POLICY.max_en_len) == (150, 2, 300)

    def test_single_engine_zero_change(self):
        # 默认 policy 的分句结果必须与不注入 policy 完全一致
        text = "她说了一句，然后停了停，又接着说，声音很低，像是在自言自语，" * 6
        assert SentenceSplitter().split(text, "zh") == \
            SentenceSplitter(policy=DEFAULT_SPLIT_POLICY).split(text, "zh")

    def test_gptsovits_policy_values(self):
        assert (GPTSOVITS_SPLIT_POLICY.max_len, GPTSOVITS_SPLIT_POLICY.min_len,
                GPTSOVITS_SPLIT_POLICY.tail_merge,
                GPTSOVITS_SPLIT_POLICY.upper_bound) == (45, 6, True, 500)

    def test_gsv_policy_caps_length_and_conserves_chars(self):
        gsv = SentenceSplitter(policy=GPTSOVITS_SPLIT_POLICY)
        text = ("这是一个非常长的句子" * 20) + "。"
        out = gsv.split(text, "zh")
        # max_len=45：所有片段不超 45（upper_bound 500 只兜更极端情况，这里 max_len 先到）
        assert all(len(s) <= 45 for s in out)
        # 字符守恒：非空白多重集不变（分句只重排/补标点，不吞汉字）
        joined = "".join(out)
        src_han = sorted(c for c in text if "\u4e00" <= c <= "\u9fff")
        out_han = sorted(c for c in joined if "\u4e00" <= c <= "\u9fff")
        assert src_han == out_han

    def test_stricter_policy_takes_strictest(self):
        merged = stricter_split_policy([DEFAULT_SPLIT_POLICY, GPTSOVITS_SPLIT_POLICY])
        assert merged == GPTSOVITS_SPLIT_POLICY
        # 仅默认 -> 默认（单引擎零变化）
        assert stricter_split_policy([DEFAULT_SPLIT_POLICY]) == DEFAULT_SPLIT_POLICY
        assert stricter_split_policy([]) == DEFAULT_SPLIT_POLICY


# ==================================================== US-B2 引号占位符原子性（缺字红线）
def _nonws(s):
    return sorted(c for c in s if not c.isspace())


class TestB2QuoteAtomicity:
    """max_len=45 会把引号保护占位符(__QUOTE_n__)从中间劈开 -> 还原失败 -> 引号内正文静默消失。

    US-B2 引入小 max_len 前该路径从未被触发（默认 150），属被暴露的既有潜在缺陷；
    成品缺字是红线，故 _chunk_overlong 必须以"完整占位符或单字符"为最小切分单元。
    """

    MIXED = (
        "Mr. Smith 说：\u201cThis is absolutely wonderful, isn't it?\u201d "
        "林昭愣了一下：\u201c博士，您认错人了。\u201d "
        "\u201c不可能，\u201d他盯着林昭的眼睛，"
        "\u201c你胸前这枚玉佩，是 Dr. Lee 留给我的。\u201d "
        "风吹过，烛火摇了摇。他缓缓坐下。\u201c你走吧。\u201d")
    DENSE = "旁白起。" + "".join(
        "他说道：\u201c第%d句很长的中文引号内容用来测试占位符原子性，这里还有逗号，继续。\u201d她点头。"
        % i for i in range(9))

    @pytest.mark.parametrize("text", [MIXED, MIXED * 3, DENSE, DENSE * 2])
    def test_gsv_tier_never_loses_quoted_text(self, text):
        out = SentenceSplitter(policy=GPTSOVITS_SPLIT_POLICY).split(text, "zh")
        joined = "".join(out)
        assert "__QUOTE_" not in joined, "占位符泄漏进成品"
        assert _nonws(joined) == _nonws(text), "非空白字符不守恒（缺字/多字）"
        assert sorted(c for c in joined if "\u4e00" <= c <= "\u9fff") == \
            sorted(c for c in text if "\u4e00" <= c <= "\u9fff")

    @pytest.mark.parametrize("text", [MIXED, MIXED * 3, DENSE])
    def test_default_tier_also_conserves(self, text):
        out = SentenceSplitter().split(text, "zh")
        joined = "".join(out)
        assert "__QUOTE_" not in joined
        assert _nonws(joined) == _nonws(text)

    def test_mutation_pre_char_index_chunker_would_have_lost_hanzi(self):
        """变异检验：按字符下标硬切（修复前口径）在这些语料上确实丢汉字。"""
        def char_index_chunk(self, sent):
            max_len, min_len = self.policy.max_len, self.policy.min_len
            if len(sent) <= max_len:
                return [sent]
            out, start, n = [], 0, len(sent)
            while n - start > max_len:
                window = sent[start:start + max_len]
                cut = -1
                for j in range(len(window) - 1, max_len // 2, -1):
                    if window[j] in self.CN_WEAK_BREAKS or window[j] in self.CN_SENTENCE_END:
                        cut = j + 1
                        break
                if cut == -1:
                    cut = max_len
                out.append(sent[start:start + cut].strip())
                start += cut
            tail = sent[start:].strip()
            if tail:
                fits = bool(out) and (len(out[-1]) + len(tail) <= max_len)
                if fits and (self.policy.tail_merge or len(tail) < min_len):
                    out[-1] += tail
                else:
                    out.append(tail)
            return [o for o in out if o]

        real = SentenceSplitter._chunk_overlong
        try:
            SentenceSplitter._chunk_overlong = char_index_chunk
            broken = SentenceSplitter(policy=GPTSOVITS_SPLIT_POLICY).split(self.MIXED, "zh")
        finally:
            SentenceSplitter._chunk_overlong = real
        assert _nonws("".join(broken)) != _nonws(self.MIXED), \
            "断言失效：字符下标硬切口径本该丢字"
        fixed = SentenceSplitter(policy=GPTSOVITS_SPLIT_POLICY).split(self.MIXED, "zh")
        assert _nonws("".join(fixed)) == _nonws(self.MIXED)

    def test_no_placeholder_path_is_byte_identical_to_char_index(self):
        """无引号语料上，单元化切分必须与逐字符旧口径逐字一致（零变化护栏）。"""
        text = ("她说了一句，然后停了停，又接着说，声音很低，像是在自言自语，谁也没有听见，" * 8) + "结束了。"
        assert "\u201c" not in text and "\u201d" not in text   # 本用例专测无引号路径
        def char_index_chunk(self, sent):
            max_len, min_len = self.policy.max_len, self.policy.min_len
            if len(sent) <= max_len:
                return [sent]
            out, start, n = [], 0, len(sent)
            while n - start > max_len:
                window = sent[start:start + max_len]
                cut = -1
                for j in range(len(window) - 1, max_len // 2, -1):
                    if window[j] in self.CN_WEAK_BREAKS or window[j] in self.CN_SENTENCE_END:
                        cut = j + 1
                        break
                if cut == -1:
                    cut = max_len
                out.append(sent[start:start + cut].strip())
                start += cut
            tail = sent[start:].strip()
            if tail:
                fits = bool(out) and (len(out[-1]) + len(tail) <= max_len)
                if fits and (self.policy.tail_merge or len(tail) < min_len):
                    out[-1] += tail
                else:
                    out.append(tail)
            return [o for o in out if o]

        for policy in (DEFAULT_SPLIT_POLICY, GPTSOVITS_SPLIT_POLICY):
            real = SentenceSplitter._chunk_overlong
            try:
                SentenceSplitter._chunk_overlong = char_index_chunk
                expect = SentenceSplitter(policy=policy).split(text, "zh")
            finally:
                SentenceSplitter._chunk_overlong = real
            assert SentenceSplitter(policy=policy).split(text, "zh") == expect


# ============================================================ US-B3
class TestB3PromptFree:
    def test_empty_prompt_text_warns_not_raises(self, net, monkeypatch):
        posts, _ = net
        eng = _adapter_with_ref(monkeypatch, prompt_text="", prompt_lang="zh")
        notices = []
        eng.set_log_sink(lambda m: notices.append(m))
        audio = eng.synthesize("测试文本。", voice_id="v1")   # 不得判硬错
        assert audio.size > 0
        assert any("prompt-free" in m for m in notices)
        # 仍然照常合成（prompt_text 原样空串下发）
        assert posts[0]["json"]["prompt_text"] == ""

    def test_nonempty_prompt_no_notice(self, net, monkeypatch):
        eng = _adapter_with_ref(monkeypatch, prompt_text="我有转写", prompt_lang="zh")
        notices = []
        eng.set_log_sink(lambda m: notices.append(m))
        eng.synthesize("测试文本。", voice_id="v1")
        assert not any("prompt-free" in m for m in notices)


# ============================================================ US-B4
class TestB4ValidateAndDegrade:
    def _ok_payload(self):
        p = dict(ga.CONTRACT_DEFAULTS)
        p.update({"text": "x", "text_lang": "zh", "ref_audio_path": "/r.wav",
                  "prompt_lang": "zh", "speed_factor": 1.0})
        return p

    def test_typo_key_rejected(self, monkeypatch):
        eng = _adapter_with_ref(monkeypatch)
        bad = self._ok_payload()
        bad["tex_lang"] = "zh"          # 拼错的键
        with pytest.raises(ValueError, match="未知参数键"):
            eng._validate_payload(bad)

    def test_enum_violation_rejected(self, monkeypatch):
        eng = _adapter_with_ref(monkeypatch)
        bad = self._ok_payload()
        bad["text_lang"] = "fr"          # 语言表外
        with pytest.raises(ValueError, match="不在允许集合"):
            eng._validate_payload(bad)

    def test_range_violation_rejected(self, monkeypatch):
        eng = _adapter_with_ref(monkeypatch)
        bad = self._ok_payload()
        bad["speed_factor"] = 9.0        # 越界
        with pytest.raises(ValueError, match="越出区间"):
            eng._validate_payload(bad)

    def test_valid_payload_passes(self, monkeypatch):
        eng = _adapter_with_ref(monkeypatch)
        eng._validate_payload(self._ok_payload())   # 不抛即通过

    def test_speed_clamp_and_bucket_close_logged(self, net, monkeypatch):
        posts, _ = net
        eng = _adapter_with_ref(monkeypatch)
        notices = []
        eng.set_log_sink(lambda m: notices.append(m))
        eng.synthesize("测试文本。", voice_id="v1", speed=2.5)   # 越界
        assert posts[0]["json"]["speed_factor"] == 2.0
        assert any("夹到" in m for m in notices)
        assert any("分桶" in m or "split_bucket" in m for m in notices)

    def test_sink_not_set_is_safe(self, net, monkeypatch):
        # 无 sink（如直连试听）时仅 warn 不崩
        eng = _adapter_with_ref(monkeypatch)
        eng.synthesize("测试文本。", voice_id="v1", speed=1.3)   # speed≠1 -> bucket close
        # 不抛即通过


# ============================================================ US-B5
def _seq():
    # A/B/A/B 交替（旁白 a / 角色 b 交错），同一 chapter
    return [
        {"index": 0, "chapter_id": "c1", "voice_id": "A", "gsv": True},
        {"index": 1, "chapter_id": "c1", "voice_id": "B", "gsv": True},
        {"index": 2, "chapter_id": "c1", "voice_id": "A", "gsv": True},
        {"index": 3, "chapter_id": "c1", "voice_id": "B", "gsv": True},
        {"index": 4, "chapter_id": "c1", "voice_id": "A", "gsv": True},
    ]


class TestB5Grouping:
    def test_is_permutation_index_equivalence(self):
        seq = _seq()
        ordered = group_gptsovits_execution_order(seq)
        # index 序列等价：同一集合、每 index 恰好一次（乱序执行不改成品顺序的前提）
        assert sorted(x["index"] for x in ordered) == [0, 1, 2, 3, 4]
        assert len(ordered) == len(seq)
        # 元素引用不变（不克隆不改写）
        assert {id(x) for x in ordered} == {id(x) for x in seq}

    def test_reduces_reference_switches(self):
        seq = _seq()
        doc = count_gsv_reference_switches(seq)              # A/B/A/B/A = 5 次
        grp = count_gsv_reference_switches(
            group_gptsovits_execution_order(seq))            # 分组后 = 2 次
        assert doc == 5
        assert grp == 2
        assert grp < doc

    def test_grouping_is_per_chapter_stable(self):
        seq = [
            {"index": 0, "chapter_id": "c1", "voice_id": "A", "gsv": True},
            {"index": 1, "chapter_id": "c2", "voice_id": "A", "gsv": True},
            {"index": 2, "chapter_id": "c1", "voice_id": "A", "gsv": True},
        ]
        ordered = group_gptsovits_execution_order(seq)
        # 不跨 chapter 合并：c1-A 的两条(0,2)相邻，c2-A(1)独立
        idx = [x["index"] for x in ordered]
        assert idx == [0, 2, 1]
        # 组内文档顺序保持（0 在 2 之前）
        assert ordered[0]["index"] == 0 and ordered[1]["index"] == 2

    def test_non_gsv_items_untouched_order(self):
        seq = [
            {"index": 0, "chapter_id": "c1", "voice_id": "E", "gsv": False},
            {"index": 1, "chapter_id": "c1", "voice_id": "A", "gsv": True},
            {"index": 2, "chapter_id": "c1", "voice_id": "E2", "gsv": False},
            {"index": 3, "chapter_id": "c1", "voice_id": "A", "gsv": True},
        ]
        ordered = group_gptsovits_execution_order(seq)
        non_gsv = [x["index"] for x in ordered if not x["gsv"]]
        assert non_gsv == [0, 2]   # 非 gsv 保序
        assert sorted(x["index"] for x in ordered) == [0, 1, 2, 3]

    def test_warm_reference_calls_set_refer_audio(self, net, monkeypatch):
        _, gets = net
        eng = _adapter_with_ref(monkeypatch)
        ok = eng.warm_reference("/abs/ref.wav")
        assert ok is True
        warm = [g for g in gets if g["url"].endswith("/set_refer_audio")][0]
        assert warm["params"] == {"refer_audio_path": "/abs/ref.wav"}

    def test_warm_reference_failure_only_warns(self, monkeypatch):
        import requests

        def boom(url, params=None, timeout=None):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(requests, "get", boom)
        eng = _adapter_with_ref(monkeypatch)
        assert eng.warm_reference("/abs/ref.wav") is False   # 不抛

    def test_warm_reference_non200_returns_false(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda url, params=None, timeout=None: _Resp(400, text="bad"))
        eng = _adapter_with_ref(monkeypatch)
        assert eng.warm_reference("/abs/ref.wav") is False


# ============================================================ 适配器契约回归
class TestContractDefaults:
    def test_cut0_and_fragment_interval(self):
        assert ga.CONTRACT_DEFAULTS["text_split_method"] == "cut0"
        assert ga.CONTRACT_DEFAULTS["fragment_interval"] == 0.02

    def test_split_policy_exposed(self):
        from modules.tts_engine.engine_registry import engine_registry
        inst = engine_registry.get("gptsovits")
        assert getattr(inst, "split_policy", None) is GPTSOVITS_SPLIT_POLICY

    def test_payload_carries_whitelisted_keys(self, net, monkeypatch):
        posts, _ = net
        eng = _adapter_with_ref(monkeypatch)
        eng.synthesize("测试文本。", voice_id="v1")
        for k in posts[0]["json"]:
            assert k in ga.TTS_PARAM_SPECS, f"payload 含白名单外键 {k}"


# ============================================================ US-B5 端到端调度
class _FakeGSV:
    engine_name = "gptsovits"
    sample_rate = 32000
    online = True

    def __init__(self, warm_fail_voices=()):
        self.synth_order = []
        self.warm_calls = []
        self._warm_fail = set(warm_fail_voices)

    def set_log_sink(self, fn):
        self._sink = fn

    def _ref_for_voice(self, vid):
        return (f"/abs/{vid}.wav", "转写", "zh")

    def warm_reference(self, ref_path):
        self.warm_calls.append(ref_path)
        for v in self._warm_fail:
            if v in ref_path:
                raise RuntimeError("预热炸了（应只 warn 不抛）")
        return True

    def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral"):
        self.synth_order.append(voice_id)
        return np.full(3200, 0.4, dtype=np.float32)


async def _drive_stage_synthesize(monkeypatch, fake, voice_seq):
    from modules.workflow import conversion_workflow as wmod
    from modules.tts_engine.engine_registry import engine_registry
    from modules.voice_manager import voice_store as vs_mod
    from modules.voice_manager import conditioning as cond_mod
    from modules.workflow import cast_pipeline_hooks as hooks_mod

    monkeypatch.setattr(engine_registry, "get", lambda name: fake)
    monkeypatch.setattr(engine_registry, "get_loaded",
                        lambda name, strict=True: fake)
    monkeypatch.setattr(vs_mod.voice_store, "get",
                        lambda vid: types.SimpleNamespace(engine="gptsovits"))
    monkeypatch.setattr(cond_mod, "resolve_profile_conditioning",
                        lambda prof, defaults=None: {"engine": "gptsovits"})
    monkeypatch.setattr(hooks_mod, "synth_params_for_sentence",
                        lambda sent, settings: {"speed": 1.0, "pitch": 0.0})

    cw = wmod.ConversionWorkflow()
    logged = []
    monkeypatch.setattr(cw, "_append_task_log",
                        lambda tid, stage, message="", extra=None, error="":
                        logged.append(message))

    async def _noop_emit(*a, **k):
        return None

    monkeypatch.setattr(cw, "_emit", _noop_emit)
    monkeypatch.setattr(cw, "is_cancelled", lambda tid: False)

    sentences = [{"text": f"第{i}句念白内容", "voice_id": v,
                  "chapter_id": "c1"} for i, v in enumerate(voice_seq)]
    ctx = {"task_id": "t-b5", "sentences": sentences, "settings": {}}
    out = await cw._stage_synthesize(ctx, {"engine": "gptsovits"})
    return out, ctx, logged


class TestB5SynthIntegration:
    async def test_grouped_path_orders_by_index_and_warms(self, monkeypatch):
        fake = _FakeGSV()
        out, ctx, logged = await _drive_stage_synthesize(
            monkeypatch, fake, ["A", "B", "A", "B", "A"])
        audio_arrays = out["audio_arrays"]
        # (2) 成品按 index 归位 —— 乱序执行不改成品顺序
        assert [seg["index"] for seg in audio_arrays] == [0, 1, 2, 3, 4]
        # (1) 走分组 elif: 同音色背靠背执行（A 全在前，不是 A/B/A/B 交替）
        assert fake.synth_order == ["A", "A", "A", "B", "B"]
        # (3) 每个 (chapter,voice) 组首预热一次 -> 2 次
        assert len(fake.warm_calls) == 2
        # 换组次数下降入账
        assert any("音色分组调度" in m for m in logged)
        assert ctx["gsv_grouping"]["doc_reference_switches"] == 5
        assert ctx["gsv_grouping"]["grouped_reference_switches"] == 2

    async def test_warm_failure_is_not_fatal(self, monkeypatch):
        fake = _FakeGSV(warm_fail_voices=("A",))   # A 组预热必抛
        out, ctx, logged = await _drive_stage_synthesize(
            monkeypatch, fake, ["A", "B", "A"])
        # 预热炸了但合成都完成，且 index 有序
        assert len(out["audio_arrays"]) == 3
        assert [seg["index"] for seg in out["audio_arrays"]] == [0, 1, 2]

    async def test_all_online_no_gsv_keeps_concurrent_path(self, monkeypatch):
        # 回归护栏：无 gptsovits 时不受分组路径影响（此处用 online 假引擎走并发分支）
        from modules.workflow import conversion_workflow as wmod
        from modules.tts_engine.engine_registry import engine_registry
        from modules.voice_manager import voice_store as vs_mod
        from modules.voice_manager import conditioning as cond_mod
        from modules.workflow import cast_pipeline_hooks as hooks_mod

        fake = _FakeGSV()
        fake.engine_name = "edgetts"   # 非 gptsovits
        monkeypatch.setattr(engine_registry, "get", lambda name: fake)
        monkeypatch.setattr(engine_registry, "get_loaded",
                            lambda name, strict=True: fake)
        monkeypatch.setattr(vs_mod.voice_store, "get",
                            lambda vid: types.SimpleNamespace(engine="edgetts"))
        monkeypatch.setattr(cond_mod, "resolve_profile_conditioning",
                            lambda prof, defaults=None: {"engine": "edgetts"})
        monkeypatch.setattr(hooks_mod, "synth_params_for_sentence",
                            lambda sent, settings: {"speed": 1.0, "pitch": 0.0})
        cw = wmod.ConversionWorkflow()
        monkeypatch.setattr(cw, "_append_task_log",
                            lambda *a, **k: None)

        async def _noop_emit(*a, **k):
            return None

        monkeypatch.setattr(cw, "_emit", _noop_emit)
        monkeypatch.setattr(cw, "is_cancelled", lambda tid: False)
        sentences = [{"text": f"句子{i}", "voice_id": "A", "chapter_id": "c1"}
                     for i in range(3)]
        ctx = {"task_id": "t-c", "sentences": sentences, "settings": {}}
        out = await cw._stage_synthesize(ctx, {"engine": "edgetts"})
        # 未分组 -> 执行顺序 = 文档顺序
        assert fake.synth_order == ["A", "A", "A"]
        assert [seg["index"] for seg in out["audio_arrays"]] == [0, 1, 2]
        assert "gsv_grouping" not in ctx   # 没走 gsv 分支
