/**
 * L14 接口消费包的前端消费面（依据 归档-非程序件/arch-upgrade/WAVE1-C-REPORT.md §跨泳道接口需求 1-4）。
 *
 * 三块职责，全部只读、绝不触发模型加载：
 *  - ModelStatusBar (L14⑦⑩)：按"这条任务实际要用的引擎"查 `model.slots[engine]`。
 *    顶层七个键是**主槽**视图（最近一次 start() 的引擎），多人有声书同时预热
 *    voxcpm + gptsovits 时顶层会"轮流说谎"，所以 slotFor 只在 primary===engine
 *    时才信任顶层。detail 分段条展示 import/weights/compile/warmup/voices，
 *    **不把 load_seconds - total_s 当排队时间**（两者口径不同源，US-C6 §风险 2 禁止）。
 *  - useVoicePreview (L14⑨)：试听排队超过 ENGINE_QUEUE_HINT_MS 才出"排队中"轻提示，
 *    心跳本地走秒（后端 snapshot 的 elapsed_seconds 只在 4s 轮询里跳，不够"活着"）。
 *  - SeedControl (L14⑧)：只对 supports_seed 的引擎出现；其余引擎连输入框都不渲染，
 *    因此请求体里连 seed 键都没有（后端对不兑现 seed 的引擎收 seed 回 400）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  detailSegments,
  ENGINE_QUEUE_HINT_MS,
  fetchVoicePreview,
  isBorrowedVoice,
  isLoadingSlot,
  seedForRequest,
  slotFor,
  supportsSeed,
  systemApi,
} from "../services/api";
import type { ModelSlot, ModelStatus, PreviewResult } from "../services/api";
import { Badge } from "./ui/Badge";
import { useI18n } from "../i18n";

/** 词典键是字面量联合；分段标签在 api.ts 的常量表里是宽 string，这里做一次收窄。 */
type DictKey = Parameters<ReturnType<typeof useI18n>["t"]>[0];
const asKey = (k: string) => k as DictKey;

const POLL_MS = 4000;

/** slots 优先；engine 未指定时退化成"看主槽"（并在 UI 上标注这是主槽口径）。 */
function slotOf(model: ModelStatus | null, engine: string | null | undefined): ModelSlot | null {
  return slotFor(model, engine || model?.primary || "");
}

/** 需求 1：轮询 model_status，并算出"任务用的那个引擎"的槽位。 */
export function useModelSlot(engine: string | null | undefined) {
  const [model, setModel] = useState<ModelStatus | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++seq.current;
    const data = await systemApi.modelStatus();
    if (mine === seq.current) setModel(data);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return { model, slot: slotOf(model, engine), refresh };
}

const STATE_LABEL: Record<string, string> = {
  idle: "voices.modelIdle",
  pending: "voices.modelPending",
  queued: "voices.modelQueued",
  loading: "voices.modelLoading",
  ready: "voices.modelReady",
  failed: "voices.modelFailed",
  skipped: "voices.modelSkipped",
};

/**
 * 需求 1+4：模型状态条。
 *
 * @param engine 这条任务/这次试听**实际要用的**引擎名；留空则显示主槽并标注。
 */
export function ModelStatusBar({
  engine,
  className = "",
}: {
  engine?: string | null;
  className?: string;
}) {
  const { t, format } = useI18n();
  const { model, slot } = useModelSlot(engine);
  const [beat, setBeat] = useState(0);
  const loading = isLoadingSlot(slot);

  useEffect(() => {
    if (!loading) {
      setBeat(0);
      return;
    }
    const id = window.setInterval(() => setBeat((x) => x + 1), 1000);
    return () => window.clearInterval(id);
  }, [loading]);

  // 探测失败（后端未起）时不占版面：状态缺失比"显示一个错的"更诚实
  if (!model) return null;

  const shownEngine = engine || model.primary || "";
  const state = slot?.state || "idle";
  const segments = detailSegments(slot?.detail);
  const queueAhead = Number(slot?.queue_ahead ?? 0);
  const elapsed: number = loading && slot?.elapsed_seconds == null ? beat : (slot?.elapsed_seconds ?? beat);

  return (
    <div
      className={`model-bar ${loading ? "model-bar--live" : ""} ${className}`}
      data-state={state}
      data-engine={shownEngine || "unknown"}
      role="status"
      aria-live="polite"
    >
      <div className="model-bar__head u-row u-gap-2">
        <span className="u-xs u-muted">{t("voices.modelFor")}</span>
        <strong className="u-xs">{shownEngine || t("common.none")}</strong>
        <span className={`u-xs model-bar__state model-bar__state--${state}`}>
          {t(asKey(STATE_LABEL[state] || "voices.modelIdle"))}
          {loading ? ` ${format.seconds(elapsed)}` : ""}
        </span>
        {!engine ? (
          <span className="u-xs u-muted" title={t("voices.modelPrimaryHint")}>
            {t("voices.modelPrimaryTag")}
          </span>
        ) : null}
        {queueAhead > 0 ? (
          <span className="u-xs model-bar__queue">
            {t("voices.modelQueueAhead", { count: queueAhead })}
          </span>
        ) : null}
      </div>

      {slot?.error ? (
        <p className="u-xs model-bar__error">{slot.error}</p>
      ) : slot?.message || slot?.detail?.message ? (
        <p className="u-xs u-muted">{slot.message || slot.detail?.message}</p>
      ) : null}

      {segments.length > 0 ? (
        <div className="model-bar__segments">
          {segments.map((seg) => (
            <div key={seg.key} className="model-bar__segment u-row u-gap-2">
              <span className="u-xs u-muted u-truncate" style={{ flex: "0 0 5em" }}>
                {t(asKey(seg.key))}
              </span>
              <span className="model-bar__segment-track" style={{ flex: 1 }}>
                <span
                  className="model-bar__segment-fill"
                  style={{ width: `${Math.round(seg.ratio * 100)}%` }}
                  aria-hidden="true"
                />
              </span>
              <span className="u-xs">{format.seconds(seg.seconds)}</span>
            </div>
          ))}
          {slot?.detail?.total_s != null ? (
            <p className="u-xs u-muted">
              {t("voices.modelTotalStage")} {format.seconds(slot.detail.total_s)}
              {slot?.load_seconds != null
                ? ` · ${t("voices.modelWallClock")} ${format.seconds(slot.load_seconds)}`
                : ""}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * 需求 3：试听排队轻提示 + 需求 2 的 seed 出口。
 *
 * 只负责"取 URL + 记 seed + 超过阈值才提示排队"，不碰 <audio> 元素，
 * 调用方拿到 PreviewResult 自己播。
 */
export function useVoicePreview() {
  const { t } = useI18n();
  const [queued, setQueued] = useState(false);
  const [running, setRunning] = useState(false);
  const [last, setLast] = useState<PreviewResult | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const clear = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => clear, [clear]);

  // 试听产物是 blob: —— 每点一次生一枚，不回收就一路攒着。
  // url 也可能是普通 http 地址，revokeObjectURL 对它是空操作，故无条件调。
  const prevUrl = useRef<string | null>(null);
  useEffect(() => {
    const stale = prevUrl.current;
    if (stale && stale !== last?.url) URL.revokeObjectURL(stale);
    prevUrl.current = last?.url ?? null;
  }, [last]);
  useEffect(
    () => () => {
      if (prevUrl.current) URL.revokeObjectURL(prevUrl.current);
      prevUrl.current = null;
    },
    []
  );

  const play = useCallback(
    async (voiceId: string): Promise<PreviewResult | null> => {
      setRunning(true);
      setQueued(false);
      setError(null);
      setPlayingId(voiceId);
      clear();
      // voxcpm 互斥闸把试听串行化（CPU 档一次真合成 3~8s）：真的等过了阈值才提示，
      // 命中已缓存的样本时不会闪一下假提示。
      timer.current = window.setTimeout(() => setQueued(true), ENGINE_QUEUE_HINT_MS);
      try {
        const res = await fetchVoicePreview(voiceId);
        setLast(res);
        return res;
      } catch (err) {
        // 后端返结构化 detail 时用它自己的话；连不上后端时 fetch 只会丢一句英文
        // TypeError("Failed to fetch") —— 那必须换成看得懂的"网络异常"，不许静默。
        setError(err instanceof TypeError ? t("common.networkError") : err instanceof Error ? err.message : String(err));
        setPlayingId((cur) => (cur === voiceId ? null : cur));
        return null;
      } finally {
        clear();
        setQueued(false);
        setRunning(false);
      }
    },
    [clear, t]
  );

  const stop = useCallback(() => {
    setPlayingId(null);
  }, []);

  return { play, stop, queued, running, last, playingId, error };
}

/**
 * 需求 2：seed 控件 —— **只在兑现 seed 的引擎出现**。
 *
 * 展示出口有两个（都只读）：试听响应头 X-Last-Successful-Seed
 * （useVoicePreview().last.seed）与 GET /api/voices/{id} 的 `seed` 键。
 * 用户填了值，调用方必须经 seedForRequest(engine, value) 再进请求体 ——
 * 门不过时它返回 undefined，JSON.stringify 后请求体里连键都没有。
 */
export function SeedControl({
  engine,
  value,
  onChange,
  lastSeed,
}: {
  engine?: string | null;
  value: string;
  onChange: (next: string) => void;
  lastSeed?: number | null;
}) {
  const { t } = useI18n();
  if (!supportsSeed(engine)) return null; // 其余引擎：连输入框都不出现，也就永远不会被 400
  const seed = seedForRequest(engine, value);
  return (
    <div className="seed-control u-row u-gap-2">
      <label className="u-xs u-muted" htmlFor="l14-seed-input">
        {t("voices.seedLabel")}
      </label>
      <input
        id="l14-seed-input"
        className="input input--sm u-xs"
        inputMode="numeric"
        value={value}
        placeholder={t("voices.seedPlaceholder")}
        onChange={(e) => onChange(e.target.value)}
      />
      {lastSeed != null ? (
        <button
          type="button"
          className="u-xs seed-control__replay"
          onClick={() => onChange(String(lastSeed))}
        >
          {t("voices.seedReplay", { seed: lastSeed })}
        </button>
      ) : null}
      {value !== "" && seed === undefined ? (
        <span className="u-xs seed-control__invalid">{t("voices.seedInvalid")}</span>
      ) : null}
    </div>
  );
}

const BINDING_LABEL: Record<string, string> = {
  tier_downgrade: "voices.bindingTierDowngrade",
  engine_fallback: "voices.bindingEngineFallback",
  proxy: "voices.bindingProxy",
};

/**
 * 需求（PY-323 a′）：**换引擎 = 换嗓子**，降级不许只写在后端日志里。
 *
 * 取值来自 `X-Preview-Voice-Binding`：`own_engine`/`native` = 原嗓子，不渲染；
 * 无头（老产物、meta 里没写）也不渲染 —— 没有证据就不编造归属。
 */
export function VoiceBindingBadge({ binding }: { binding?: string | null }) {
  const { t } = useI18n();
  if (!isBorrowedVoice(binding)) return null;
  const key = BINDING_LABEL[binding as string];
  return (
    <Badge tone="warning" title={binding ?? ""}>
      {key ? t(asKey(key)) : t("voices.bindingUnknown", { binding: binding ?? "" })}
    </Badge>
  );
}
