"""U-3 / S2 的静态守卫：/voices 的渲染量必须与音色库总量解耦。

为什么用读源码的字面量守卫而不是 DOM 断言：本仓没有 JS 测试运行器，而这条
缺陷（1039 张卡 / 1039 个 `<audio>` / 单条 3,642ms long task）恰好是"写回去
就复发"的那一类——把形态钉成断言，改回旧写法必红。真机侧的 long task 与节点
计数由 qa 台账记录（同一会话 A/B，见 LEDGER §16）。

三段结构：① 前置基线（PAGE_SIZE 有界、i18n 两侧条数相等）② 形态守卫（窗口
渲染、`<audio>` 门控、blob 回收）③ 方向核对（红必须红在想复的那个点上）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "src" / "routes" / "VoiceStudioPage.tsx"
PREVIEW = ROOT / "frontend" / "src" / "components" / "ModelStatusBar.tsx"
ZH = ROOT / "frontend" / "src" / "i18n" / "zh.ts"
EN = ROOT / "frontend" / "src" / "i18n" / "en.ts"

LEAF = re.compile(r"^\s+[A-Za-z][A-Za-z0-9_]*:\s", re.M)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _page_size() -> int:
    m = re.search(r"^const PAGE_SIZE = (\d+);", _read(PAGE), re.M)
    assert m, "PAGE_SIZE 常量不见了：窗口渲染失去上界，等于回到全量提交"
    return int(m.group(1))


# ------------------------------------------------- ① 前置基线：窗口必须有上界
def test_grid_has_a_bounded_window():
    size = _page_size()
    assert 1 <= size <= 120, f"PAGE_SIZE={size} 已超出可接受的单页节点量级"


def test_i18n_keys_stay_paired():
    zh, en = _read(ZH), _read(EN)
    assert len(LEAF.findall(zh)) == len(LEAF.findall(en)), (
        "zh/en 文案条数不再相等：新增键漏了另一边，界面上会出现裸键名"
    )


# ------------------------- ② 形态守卫：只渲染窗口、只挂一枚播放器、blob 要回收
def test_grid_renders_the_window_not_the_whole_list():
    src = _read(PAGE)
    assert "pageVoices.map((v) =>" in src, (
        "网格又改成整表 map：1039 条音色会一次性提交 33,207 个节点"
    )
    assert "const pageVoices = useMemo" in src
    assert re.search(r"voices\.slice\(\(page - 1\) \* PAGE_SIZE", src), "窗口切片丢失"


def test_audio_element_is_mounted_only_for_the_playing_card():
    src = _read(PAGE)
    # 卡片脚部那枚 <audio> 必须被 playingId 三元式门住；旧写法是无条件 AudioRow
    assert "preview.playingId === v.id ? (" in src, "播放器失去 playingId 门控"
    assert "<AudioRow src={voiceApi.previewUrl(v.id)} title={v.name} />" not in src, (
        "每张卡又无条件挂了一枚 <AudioRow>：整页就是 PAGE_SIZE 个媒体元素"
    )


def test_pager_exists_and_is_rendered_only_when_needed():
    src = _read(PAGE)
    assert "const totalPages = Math.max(1, Math.ceil(voices.length / PAGE_SIZE))" in src
    assert "pageVoices.map" in src and "voices.pageNext" in src


def test_preview_blob_url_is_revoked():
    src = _read(PREVIEW)
    assert "URL.revokeObjectURL" in src, (
        "试听产物是 blob: —— 不回收就每点一次攒一枚，整页试听后堆几十份音频"
    )
    body = src[src.index("export function useVoicePreview"):]
    assert "revokeObjectURL(stale)" in body, "替换播放时没有回收上一枚"
    assert "revokeObjectURL(prevUrl.current)" in body, "组件卸载时没有回收在用的那枚"


def test_hook_exposes_which_voice_is_playing():
    src = _read(PREVIEW)
    assert "playingId" in src and "return { play, stop, queued, running, last, playingId" in src


# ------------------------- ③ 方向核对：新增的 i18n 键两侧都在，且形态一致
NEW_KEYS = ["pageInfo", "pagePrev", "pageNext", "pageFirst", "pageLast", "previewStop"]


def test_pager_keys_exist_on_both_sides():
    zh, en = _read(ZH), _read(EN)
    for key in NEW_KEYS:
        assert re.search(rf"^\s+{key}: ", zh, re.M), f"zh 缺 voices.{key}"
        assert re.search(rf"^\s+{key}: ", en, re.M), f"en 缺 voices.{key}"


def test_page_info_is_interpolated_not_hardcoded():
    """翻页提示必须走 t() 插值：写死中文就是红线②（双语一致）的回退。

    只查 JSX 文本节点 —— 注释里出现"下一页"这类词是合法的，不算硬编码。
    """
    src = _read(PAGE)
    assert 't("voices.pageInfo", { page, total: totalPages })' in src
    for text in ["上一页", "下一页", "首页", "末页", "停止试听"]:
        assert f">{text}<" not in src, f"界面文案里硬编码了 {text!r}"
