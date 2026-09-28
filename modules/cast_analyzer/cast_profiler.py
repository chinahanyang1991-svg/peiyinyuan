"""
cast 画像（多人有声书批次 A）

设计依据: specs/2026-09-20-multi-cast-audiobook-design.md 4.1

- 输入 CastCandidate + 样句 -> {gender, age_group, identity, personality_tags[]}
- 批量: 每次 <=20 角色、每人 <=3 条样句, 一次 LLM 调用出 JSON; 失败重试 1 次
- LLM 不可用 (is_ready() == False) 或调用失败 -> 整表/单角色走 _fallback_profile,
  任务绝不抛错 (spec 4.1: "不可走规则兜底", spec 11: unknown 由 UI 标 "待完善")
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

VALID_GENDERS = ("male", "female", "neutral", "unknown")
VALID_AGES = ("child", "young", "adult", "senior", "unknown")

SYSTEM_PROMPT = """你是中文长篇小说的角色分析助手。给定若干角色的对白样句，推断每个角色的属性。

规则：
1. 只输出 JSON 数组，不要任何解释、不要 markdown 代码块
2. 每个元素形如：
   {"name": "角色名", "gender": "male|female|neutral", "age_group": "child|young|adult|senior",
    "identity": "职业或身份，例如 队长/学生/医生", "personality_tags": ["沉稳", "幽默"]}
3. gender 只能取 male/female/neutral；age_group 只能取 child/young/adult/senior
4. 无法判断时 identity 填 "unknown"，personality_tags 给空数组
5. name 必须与输入的候选名完全一致，不得新增或改名"""

# ---------------------------------------------------------------- 规则兜底词表
# 只放**名词性称谓**：裸代词 他/她 由抽取层按"名字所在叙述窗口"算好（he_count/she_count），
# 在这里再数一遍等于把"她在谈他"记成她的性别（PY-334）。同一条目不得重复——
# 计数用 text.count()，重复词会双计权重。
MALE_WORDS = ("先生", "大哥", "大爷", "队长", "叔叔", "伯伯", "男人", "丈夫", "父亲",
              "小哥", "少爷", "将军", "老板", "师傅")
FEMALE_WORDS = ("女士", "小姐", "阿姨", "大妈", "女人", "妻子", "母亲", "姑娘",
                "妹妹", "姐姐", "婶子", "嫂子", "奶奶")
CHILD_WORDS = ("孩子", "小孩", "少年", "少女", "小学", "儿子", "女儿", "弟弟", "妹妹",
               "几岁", "童年", "小娃娃", "奶娃")
YOUNG_WORDS = ("青年", "年轻", "大学生", "小伙子", "姑娘", "新人", "实习", "小妹", "小哥")
SENIOR_WORDS = ("老人", "老大爷", "年老", "上了年纪", "白发", "苍老", "退休", "爷爷",
                "奶奶", "长辈", "年迈", "晚年", "老朽", "老先生", "老师太")
# "小满/阿贵" 这类称谓常指年轻晚辈，只作低置信提示
YOUNG_NAME_PREFIXES = ("小", "阿")
IDENTITY_WORDS = [
    "队长", "副队长", "老师", "教师", "医生", "大夫", "护士", "船长", "掌柜", "老板",
    "经理", "董事长", "局长", "站长", "部长", "教授", "馆长", "警察", "警官", "侦探",
    "记者", "律师", "师傅", "将军", "司令", "团长", "连长", "参谋", "主编", "主任",
    "院长", "校长", "学生", "士兵", "农民", "工人", "商人", "厨师", "演员", "作家",
    "诗人", "县委书记", "村长", "支书", "司机", "快递员", "程序员",
]
PERSONALITY_WORDS = [
    "沉稳", "磁性", "温和", "温柔", "活泼", "幽默", "严厉", "急躁", "内向", "外向",
    "正直", "圆滑", "倔强", "乐观", "悲观", "果断", "犹豫", "傲慢", "谦逊", "泼辣",
    "老练", "天真", "冷酷", "热情", "寡言", "健谈", "怯懦", "勇敢",
]


class CastProfiler:
    """角色画像器：LLM 批量 + 规则全表兜底。"""

    def __init__(self, llm_service=None, batch_size: int = 20, quotes_per_role: int = 3,
                 max_retries: int = 1):
        """
        Args:
            llm_service: LLMService 实例或 None。None 时按 needs 惰性构造；
                         构造失败即视为不可用，整表走 fallback。
            batch_size: 单次 LLM 调用最多处理的候选角色数
            quotes_per_role: 每个角色送样句条数
            max_retries: 批量调用失败后的重试次数
        """
        self._llm = llm_service
        self._llm_explicit = llm_service is not None
        self.batch_size = max(1, int(batch_size))
        self.quotes_per_role = max(1, int(quotes_per_role))
        self.max_retries = max(0, int(max_retries))
        self.last_error: Optional[str] = None
        self.llm_calls = 0
        self.llm_batch_ok = 0
        self.fallback_roles = 0

    # --------------------------------------------------------------- LLM 可用性
    def llm_ready(self) -> bool:
        """LLM 侧车是否可用；任何异常都视为不可用（不许抛错）。"""
        svc = self._ensure_service()
        if svc is None:
            return False
        try:
            return bool(svc.is_ready())
        except Exception as exc:  # pragma: no cover - 防御
            self.last_error = f"is_ready 异常: {type(exc).__name__}: {exc}"
            return False

    def _ensure_service(self):
        if self._llm is not None or self._llm_explicit:
            return self._llm
        try:
            from modules.llm_client.llm_service import LLMService
            self._llm = LLMService()
        except Exception as exc:
            self._llm_explicit = True   # 不再重试构造
            self._llm = None
            self.last_error = f"LLMService 构造失败: {type(exc).__name__}: {exc}"
        return self._llm

    # --------------------------------------------------------------- 主流程
    def profile(self, candidates: Sequence) -> List:
        """
        就地回填 candidate.profile 并返回同一列表。
        profile = {gender, age_group, identity, personality_tags, source, confidence}
        """
        items = [self._as_view(c) for c in candidates]
        real = [it for it in items if not it["is_narrator"]]

        ready = self.llm_ready()
        by_name: Dict[str, Dict] = {}
        if ready:
            for start in range(0, len(real), self.batch_size):
                batch = real[start:start + self.batch_size]
                for name, prof in self._llm_batch(batch).items():
                    by_name[name] = prof

        for view in real:
            prof = by_name.get(view["name"])
            if prof is None:
                prof = self._fallback_profile(view)
                self.fallback_roles += 1
            view["profile"] = prof

        # 旁白固定画像（不走 LLM，省一次调用；也让 voice_matcher 有确定性输入）
        for view in items:
            if view["is_narrator"]:
                view["profile"] = {
                    "gender": "neutral", "age_group": "adult",
                    "identity": "旁白", "personality_tags": ["沉稳", "磁性", "专业"],
                    "source": "builtin_narrator", "confidence": 1.0,
                }

        for cand, view in zip(candidates, items):
            self._write_back(cand, view["profile"])
        return list(candidates)

    # --------------------------------------------------------------- LLM 批量
    def _llm_batch(self, batch: List[Dict]) -> Dict[str, Dict]:
        payload = [{
            "name": v["name"],
            "aliases": v["aliases"][:4],
            "lines": v["lines_count"],
            "quotes": v["quotes"][: self.quotes_per_role],
        } for v in batch]
        user = "请分析以下角色：\n" + json.dumps(payload, ensure_ascii=False)
        attempts = self.max_retries + 1
        for _ in range(attempts):
            self.llm_calls += 1
            svc = self._ensure_service()
            try:
                raw = svc.client.chat(
                    messages=[{"role": "user", "content": user}],
                    system_prompt=SYSTEM_PROMPT,
                    temperature=0.1,
                )
            except Exception as exc:
                self.last_error = f"chat 异常: {type(exc).__name__}: {exc}"
                continue
            parsed = self._parse_json(raw)
            if parsed:
                out: Dict[str, Dict] = {}
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    prof = self._normalize(item)
                    if prof:
                        out[name] = prof
                if out:
                    self.llm_batch_ok += 1
                    return out
            self.last_error = "LLM 返回无法解析为角色画像 JSON"
        return {}

    @staticmethod
    def _parse_json(raw) -> List[Dict]:
        if not raw:
            return []
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        text = re.sub(r"^```\w*\s*|\s*```$", "", text.strip())
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                return []
            try:
                data = json.loads(m.group())
            except Exception:
                return []
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []

    @staticmethod
    def _normalize(item: Dict) -> Optional[Dict]:
        gender = str(item.get("gender") or "").strip().lower()
        if gender not in ("male", "female", "neutral"):
            gender = "unknown"
        age = str(item.get("age_group") or "").strip().lower()
        age_map = {"child": "child", "young": "young", "adult": "adult", "senior": "senior",
                   "middle": "adult", "elderly": "senior", "teen": "young"}
        age = age_map.get(age, "unknown")
        tags = item.get("personality_tags") or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,，、\s]+", tags) if t]
        tags = [str(t).strip() for t in tags if str(t).strip()][:6]
        identity = str(item.get("identity") or "unknown").strip() or "unknown"
        if gender == "unknown" and age == "unknown" and not tags:
            return None
        return {
            "gender": gender, "age_group": age, "identity": identity,
            "personality_tags": tags, "source": "llm", "confidence": 0.8,
        }

    # --------------------------------------------------------------- 规则兜底
    def _fallback_profile(self, view: Dict) -> Dict:
        """代词统计 -> 性别；年龄/身份/性格词表 -> 其余属性。"""
        name = view["name"]
        quotes = view["quotes"]
        aliases = view["aliases"]
        text = " ".join(quotes + aliases + [name])
        # 性别只认**叙述侧**证据：抽取层按"名字所在窗口"算好的 he/she + 名字/别名/称谓里的
        # 身份名词。台词里出现的 他/她 多半是角色在谈**别人**（"把她关起来"≠她是女的），
        # 把它计进来会让实盘 61 枚说话角色 58 枚判成 male（PY-334）。
        # 年龄/身份/性格仍读台词——那些确实是角色自己的属性。
        gender_text = " ".join(aliases + [name] + list(view["titles"]))

        he = view["he_count"]
        she = view["she_count"]
        male = he + sum(gender_text.count(w) for w in MALE_WORDS)
        female = she + sum(gender_text.count(w) for w in FEMALE_WORDS)
        if male > female:
            gender = "male"
        elif female > male:
            gender = "female"
        else:
            gender = "unknown"

        scores = {
            "child": sum(text.count(w) for w in CHILD_WORDS),
            "young": sum(text.count(w) for w in YOUNG_WORDS),
            "senior": sum(text.count(w) for w in SENIOR_WORDS),
        }
        best_age = max(scores, key=lambda k: scores[k])
        if scores[best_age] > 0:
            age_group = best_age
            confidence = min(0.7, 0.35 + 0.1 * scores[best_age])
        elif name[:1] in YOUNG_NAME_PREFIXES:
            age_group = "young"          # 小满 / 阿贵 这类称谓：低置信年轻
            confidence = 0.3
        elif view["lines_count"] >= 6:
            age_group = "adult"          # 戏份足但无年龄线索，按成年人处理
            confidence = 0.4
        else:
            age_group = "unknown"
            confidence = 0.25

        identity = "unknown"
        if view["titles"]:
            identity = view["titles"][0]     # 归属统计到的称谓最可靠
        if identity == "unknown":
            for word in IDENTITY_WORDS:
                if word in name:
                    identity = word
                    break
        if identity == "unknown":
            m = re.search(r"我(?:是|乃|本是|就是)([\u4e00-\u9fa5]{1,6})", " ".join(quotes))
            if m:
                identity = m.group(1).strip("们的手里个")

        tags = [w for w in PERSONALITY_WORDS if w in text][:5]
        return {
            "gender": gender,
            "age_group": age_group,
            "identity": identity,
            "personality_tags": tags,
            "source": "fallback",
            "confidence": round(confidence, 2),
        }

    # --------------------------------------------------------------- 视图/回填
    def _as_view(self, cand) -> Dict:
        """CastCandidate 对象或 dict 统一成内部视图。"""
        get = self._getter(cand)
        return {
            "name": get("name", "") or "",
            "aliases": list(get("aliases", []) or []),
            "quotes": list(get("sample_quotes", []) or []),
            "titles": list(get("titles", []) or []),
            "lines_count": int(get("lines_count", 0) or 0),
            "he_count": int(get("he_count", 0) or 0),
            "she_count": int(get("she_count", 0) or 0),
            "it_count": int(get("it_count", 0) or 0),
            "is_narrator": bool(get("is_narrator", False)),
            "profile": {},
        }

    @staticmethod
    def _write_back(cand, profile: Dict) -> None:
        if isinstance(cand, dict):
            cand["profile"] = profile
        else:
            try:
                cand.profile = profile
            except Exception:  # pragma: no cover - 冻结对象等极端情况
                pass

    @staticmethod
    def _getter(obj):
        if isinstance(obj, dict):
            return lambda k, d=None: obj.get(k, d)
        return lambda k, d=None: getattr(obj, k, d)

    # --------------------------------------------------------------- 元信息
    def stats(self) -> Dict:
        return {
            "llm_ready": self.llm_ready(),
            "llm_calls": self.llm_calls,
            "llm_batch_ok": self.llm_batch_ok,
            "fallback_roles": self.fallback_roles,
            "last_error": self.last_error,
        }


def profiles_to_role_extra_data(candidates: Sequence) -> List[Dict]:
    """
    批次 B 写库用的形状：Role.extra_data。
    仅产出 dict，不碰 DB（spec 3: 属性/别名全存 extra_data，无 schema 迁移）。
    """
    out: List[Dict] = []
    for cand in candidates:
        get_name = CastProfiler._getter(cand)
        profile = get_name("profile", {}) or {}
        out.append({
            "name": get_name("name", ""),
            "role_type": get_name("role_type", "supporting"),
            "voice_id": get_name("voice_id", None),
            "aliases": get_name("aliases", []) or [],
            "cast": {
                "lines_count": get_name("lines_count", 0),
                "occurrences": get_name("occurrences", 0),
                "direct_hints": get_name("direct_hints", 0),
                "chapters_touched": get_name("chapters_touched", []) or [],
                "chapters_count": get_name("chapters_count", 0),
                "co_speakers": get_name("co_speakers", []) or [],
                # CAST-RESIDUAL: 第 5 层包含式归并的可复核判定（含未归并的歧义主机）
                "alias_candidates": get_name("alias_candidates", []) or [],
                "sample_quotes": get_name("sample_quotes", []) or [],
                "gender": profile.get("gender", "unknown"),
                "age_group": profile.get("age_group", "unknown"),
                "identity": profile.get("identity", "unknown"),
                "personality_tags": profile.get("personality_tags", []) or [],
                "profile_source": profile.get("source", "unknown"),
                "profile_confidence": profile.get("confidence", 0.0),
            },
            "mix": {"gain_db": 0.0, "speed": 1.0, "pitch": 0.0},
        })
    return out
