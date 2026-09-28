/**
 * API 服务层
 * 封装所有后端 API 调用
 */

import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 60000,  // 延长超时，音色分析/下载可能耗时
  headers: { 'Content-Type': 'application/json' },
});

// ========== 电子书 API ==========
export const bookApi = {
  import: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post('/books/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  list: () => api.get('/books/'),
  get: (id: string) => api.get(`/books/${id}`),
  getChapters: (id: string) => api.get(`/books/${id}/chapters`),
  getChapterContent: (chapterId: string) =>
    api.get(`/books/chapters/${chapterId}/content`),
  updateChapters: (id: string, chapters: any[]) =>
    api.put(`/books/${id}/chapters`, chapters),
  updateChapter: (chapterId: string, data: { title?: string; content?: string; index?: number }) =>
    api.patch(`/books/chapters/${chapterId}`, data),
  mergeChapters: (bookId: string, chapterIds: string[], newTitle: string) =>
    api.post(`/books/${bookId}/chapters/merge`, { chapter_ids: chapterIds, new_title: newTitle }),
  splitChapter: (chapterId: string, positions: number[]) =>
    api.post(`/books/chapters/${chapterId}/split`, { positions }),
  delete: (id: string) => api.delete(`/books/${id}`),
};

// ========== 音色 API ==========
/** 后端 `/voices/` 的 limit 默认 100、上限 500（`Query(le=500)`），而库里已 500+ 条：
 *  任何"要整张库"的消费方都必须按 total 翻页，只吃首页会让合法音色被误判为不存在（UI-100 L1）。 */
const VOICE_PAGE_LIMIT = 200;
/** 翻页硬护栏：25 × 200 = 5000 条，防后端 total 异常时死循环 */
const VOICE_MAX_PAGES = 25;

export const voiceApi = {
  list: (params?: any) => api.get('/voices/', { params }),
  /**
   * total 感知的全量拉取：limit/offset 逐页取满 `data.total`（后端 WF-12 已补 total/offset）。
   * 后端契约缺 total 时（旧版/代理裁剪）退化为"本页不满 limit 即到底"；返回合并去重后的音色数组。
   * 失败时抛出 axios 错误，由调用方沿用各自的错误态处理。
   */
  listAll: async (params: any = {}, pageLimit = VOICE_PAGE_LIMIT): Promise<any[]> => {
    const limit = Math.min(Math.max(Math.floor(pageLimit) || VOICE_PAGE_LIMIT, 1), 500);
    const acc: any[] = [];
    const seen = new Set<string>();
    for (let page = 0; page < VOICE_MAX_PAGES; page += 1) {
      const res = await api.get('/voices/', { params: { ...params, limit, offset: page * limit } });
      const rows: any[] = res.data?.voices ?? [];
      rows.forEach((v, i) => {
        const key = String(v?.id ?? v?.voice_id ?? `anon:${page}:${i}`);
        if (seen.has(key)) return;
        seen.add(key);
        acc.push(v);
      });
      if (rows.length === 0) break;
      const total = Number(res.data?.total);
      const hasTotal = Number.isFinite(total) && total >= 0;
      if (hasTotal ? acc.length >= total : rows.length < limit) break;
    }
    return acc;
  },
  get: (id: string) => api.get(`/voices/${id}`),
  /** { total, by_source, by_engine }（U-11：引擎筛选选项的唯一数据源，不再由前端硬编码） */
  statistics: () => api.get('/voices/statistics'),
  previewUrl: (id: string) => `/api/voices/${id}/preview`,
  clone: (audio: File, name: string, transcript?: string) => {
    const formData = new FormData();
    formData.append('audio', audio);
    formData.append('name', name);
    if (transcript) formData.append('transcript', transcript);
    return api.post('/voices/clone', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  analyze: (audio: File, name?: string) => {
    const formData = new FormData();
    formData.append('audio', audio);
    if (name) formData.append('name', name);
    return api.post('/voices/analyze', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  analyzeAndSave: (audio: File, name: string, language: string = 'zh') => {
    const formData = new FormData();
    formData.append('audio', audio);
    formData.append('name', name);
    formData.append('language', language);
    return api.post('/voices/analyze-and-save', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  design: (description: string, name: string) =>
    api.post('/voices/design', { description, name }),
  searchOnline: (query: string, language?: string) =>
    api.get('/voices/online/search', { params: { query, language } }),
  download: (modelName: string, engine: string) =>
    api.post('/voices/online/download', { model_name: modelName, engine }),
  delete: (id: string) => api.delete(`/voices/${id}`),
};

// ========== 配音 API ==========
export const dubbingApi = {
  createProject: (data: any) => api.post('/dubbing/projects', data),
  listProjects: () => api.get('/dubbing/projects'),
  getProject: (id: string) => api.get(`/dubbing/projects/${id}`),
  updateAssignments: (id: string, data: any) =>
    api.put(`/dubbing/projects/${id}/assignments`, data),
  preview: (id: string) => api.post(`/dubbing/projects/${id}/preview`),
  listRoles: (id: string) => api.get(`/dubbing/projects/${id}/roles`),
  addRole: (id: string, data: { name: string; role_type?: string }) =>
    api.post(`/dubbing/projects/${id}/roles`, data),
  deleteRole: (id: string, roleId: string) =>
    api.delete(`/dubbing/projects/${id}/roles/${roleId}`),
  assignVoice: (id: string, data: { role_id: string; voice_id: string }) =>
    api.put(`/dubbing/projects/${id}/roles/assign-voice`, data),
  updateSettings: (id: string, settings: any) =>
    api.put(`/dubbing/projects/${id}/settings`, { settings }),
};

// ========== 多人有声书：角色分析 / 音色推荐 API（backend/api/cast.py） ==========
export interface CastProfile {
  gender?: string;
  age_group?: string;
  identity?: string;
  personality_tags?: string[];
  profile_source?: string;
  profile_confidence?: number;
  lines_count?: number;
  chapters_count?: number;
  chapters_touched?: number[];
  co_speakers?: Record<string, number>;
  sample_quotes?: string[];
}

export interface CastRole {
  id: string;
  project_id: string;
  name: string;
  role_type: string;
  voice_id: string | null;
  aliases: string[];
  mix: { gain_db?: number; speed?: number; pitch?: number };
  cast: CastProfile;
  confirmed: boolean;
  extra_data?: Record<string, any>;
}

export interface VoiceSuggestion {
  voice_id: string;
  voice_name: string;
  engine?: string;
  source?: string;
  gender?: string;
  age_group?: string;
  score: number;
  breakdown: { gender: number; age: number; style: number; language: number };
}

export interface CastSnapshot {
  status: string;
  project_id: string;
  mode: string;
  book_id: string | null;
  analyzed: boolean;
  confirmed: boolean;
  narrator: string | null;
  cast: {
    status?: string;
    aliases?: Record<string, string>;
    narrator?: string | null;
    stats?: Record<string, any>;
    speaker_map?: Record<string, { role_id: string; voice_id: string | null }>;
  };
  roles: CastRole[];
  role_count: number;
  character_count: number;
  mix: MixSettings;
  gaps?: Record<string, number>;
}

export interface BgmConf {
  ref?: string | null;
  gain_db?: number;
  duck_db?: number;
  ducking?: boolean;
  loopable?: boolean;
}

export interface MixSettings {
  bgm?: BgmConf;
  sfx?: {
    items?: { at: string | number; file: string; gain_db?: number }[];
    chapter_marks?: boolean;
    start_ref?: string;
    end_ref?: string;
    start_gain_db?: number;
    end_gain_db?: number;
  };
  role_gains?: Record<string, { gain_db?: number }>;
  master?: { gain_db?: number; target_lufs?: number | null; ceiling_db?: number };
  gaps?: { dialogue?: number; narration?: number; default?: number };
  chapter_overrides?: Record<string, Partial<MixSettings>>;
}

export interface BgmItem {
  id: string;
  name: string;
  file?: string;
  kind?: string;
  moods?: string[];
  duration_s?: number;
  loopable?: boolean;
  builtin?: boolean;
  placeholder?: boolean;
  source?: string;
  external?: boolean;
}

/** FastAPI 的 detail 可能是字符串，也可能是 {reason, model_status, next} 结构体；
 *  直接塞进 Error 会渲染成 "[object Object]"，这里统一扁平成一行可读文案。 */
export function detailText(detail: unknown, fallback = ''): string {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    const parts = [d.reason, d.message, d.detail, d.next]
      .filter((x): x is string => typeof x === 'string' && !!x.trim());
    if (parts.length) return parts.join(' — ');
  }
  return fallback;
}

export const castApi = {
  /** 启动角色分析后台任务：返回 task.id，进度走 /api/tasks/{id}/progress */
  analyze: (id: string, data?: { book_id?: string; use_llm?: boolean }) =>
    api.post(`/dubbing/projects/${id}/analyze-cast`, data || {}),
  get: (id: string) => api.get<CastSnapshot>(`/dubbing/projects/${id}/cast`),
  suggestions: (id: string, top_n = 5, language?: string) =>
    api.get(`/dubbing/projects/${id}/cast/suggestions`, { params: { top_n, language } }),
  confirm: (id: string, data: {
    roles: { role_id?: string; name?: string; role_type?: string;
             voice_id?: string | null; aliases?: string[];
             gender?: string; age_group?: string; identity?: string;
             personality_tags?: string[]; mix?: Record<string, any> }[];
    auto_assign_rest?: boolean;
  }) => api.put(`/dubbing/projects/${id}/cast/confirm`, data),
  updateRole: (id: string, roleId: string, patch: Record<string, any>) =>
    api.put(`/dubbing/projects/${id}/cast/roles/${roleId}`, patch),
  /** 按角色试听：引擎未加载时后端给 503 + 指引（不返回静音冒充成品） */
  preview: async (id: string, body: { role_id?: string; role_name?: string;
                                      text?: string; max_sentences?: number }) =>
    (await castApi.previewWithMeta(id, body)).url,
  /** 与 preview 同一条真路径，但把 US-C4 的出口（响应头 seed）交给调用方 (L14⑧) */
  previewWithMeta: async (id: string, body: { role_id?: string; role_name?: string;
                                              text?: string; max_sentences?: number;
                                              seed?: number | null }): Promise<PreviewResult> => {
    // 引擎不兑现 seed 时**连字段都不发**（后端对不支持引擎收 seed 回 400）
    const sent = { ...body };
    if (sent.seed === undefined || sent.seed === null) delete sent.seed;
    const res = await fetch(`/api/dubbing/projects/${id}/cast/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sent),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      // detail 可能是 {reason, model_status, next} 结构体，直出会是 "[object Object]"
      throw new PreviewUnavailableError(
        detailText(err?.detail, `试听失败 (${res.status})`),
        queueHintFromDetail(err?.detail),
      );
    }
    return {
      url: URL.createObjectURL(await res.blob()),
      seed: readSeedHeader(res),
      voiceBinding: readVoiceBindingHeader(res),
    };
  },
};

// ============================================================ L14 接口消费包
// 依据：归档-非程序件/arch-upgrade/WAVE1-C-REPORT.md §跨泳道接口需求 1-4（后端由 US-C1/C4/C5/C6 落地）。

/** US-C6 `snapshot()["detail"]` 的分段计时（CPU 档 compile/warmup 恒 null）。 */
export interface ModelLoadDetail {
  device?: string | null;
  optimize?: boolean | null;
  import_s?: number | null;
  weights_s?: number | null;
  compile_s?: number | null;
  warmup_s?: number | null;
  restore_voices_s?: number | null;
  total_s?: number | null;
  skipped_stage?: string | null;
  message?: string | null;
}

/** `model_preloader.EngineSlot.view()` 的键（modules/tts_engine/model_preloader.py）。
 *  `attempts` / `retry_limit` / `gate` 为 #86 ENG-PRELOAD 新增：装载尝试次数（含带上限的
 *  重试）与"内存门是否生效 + 观测到的可用内存"，失败归因不再靠 `skipped_reason` 猜。 */
export interface ModelSlot {
  state?: string | null;
  engine?: string | null;
  load_seconds?: number | null;
  elapsed_seconds?: number | null;
  error?: string | null;
  skipped_reason?: string | null;
  queue_ahead?: number | null;
  detail?: ModelLoadDetail | null;
  attempts?: number | null;
  retry_limit?: number | null;
  gate?: Record<string, unknown> | null;
  message?: string | null;
}

/** `/api/system/model_status` 的 `model`：顶层七键 = **主槽**视图 + 增量 `slots`/`primary`。 */
export interface ModelStatus extends ModelSlot {
  primary?: string | null;
  slots?: Record<string, ModelSlot> | null;
}

export interface PreviewResult {
  url: string;
  /** US-C4 出口：本次试听实际生效的 seed；引擎不兑现 seed 时后端**不发头** => null */
  seed: number | null;
  /**
   * 这一耳朵听到的是**谁的嗓子**（`X-Preview-Voice-Binding`）。
   * 后端三态如实标：`own_engine`/`native` = 就是这个音色声明的嗓子；
   * `proxy` = 速度档挑的代理声；`engine_fallback` = 声明引擎加载失败回落；
   * `tier_downgrade` = 参考音频语种档不合格被换引擎（PY-323 a′，换引擎=换嗓子）。
   * 后端未发头（老产物/无 meta）时 null —— 不猜成 own_engine。
   */
  voiceBinding: string | null;
}

/** 503 的 `{reason, model_status, next}` 结构体 + 排队判定，供 UI 出轻提示 (需求 3)。 */
export class PreviewUnavailableError extends Error {
  readonly queueHint: ModelSlot | null;
  constructor(message: string, queueHint: ModelSlot | null = null) {
    super(message);
    this.name = 'PreviewUnavailableError';
    this.queueHint = queueHint;
  }
}

/** US-C5 互斥闸把试听变成串行等待（CPU 档一次真合成 3~8s）：超过这个时长就给"排队中"提示。 */
export const ENGINE_QUEUE_HINT_MS = 1500;

/** 试听响应头（`backend/api/voices.py:101`、`dubbing.py:202`、`cast.py:330` 同一出口）。 */
export const SEED_HEADER = 'X-Last-Successful-Seed';

export const systemApi = {
  health: () => api.get('/health'),
  info: () => api.get('/system/info'),
  /** 两档配音模式 + 全部引擎端口（只读元数据，后端不联网/不加载权重）。 */
  engines: () => api.get('/system/engines'),
  /** 需求 1：模型状态单独取（顶层主槽会"轮流说谎"，务必配 slotFor 用）。 */
  modelStatus: async (): Promise<ModelStatus | null> => {
    try {
      const res = await api.get('/system/model_status');
      return (res.data?.model ?? null) as ModelStatus | null;
    } catch (err) {
      // 返回 null 是刻意的：状态探测绝不拖垮页面。留一行控制台痕迹，
      // 免得"状态条一直是空"这种现场只能靠猜是后端没起还是请求挂了。
      console.warn('读取模型状态失败，状态条按"未知"渲染:', err);
      return null;
    }
  },
};

/**
 * 需求 1：**按任务实际要用的引擎**查 `model.slots[engine]`。
 *
 * 顶层七个键是"最近一次 start() 的那个引擎"（主槽）视图，多人有声书同时预热
 * voxcpm + gptsovits 时顶层会轮流说谎 —— 只看顶层会出现"显示 ready、任务仍在等
 * 另一个引擎"。因此：命中 slots 就用 slots；只有当顶层 primary 就是这个引擎时
 * 顶层才是它的视图；都没有则 null（= 该引擎从未 start 过，语义是 idle）。
 */
export function slotFor(model: ModelStatus | null | undefined,
                        engine: string | null | undefined): ModelSlot | null {
  if (!model || !engine) return null;
  const slot = model.slots?.[engine];
  if (slot) return slot;
  if (model.primary && model.primary !== engine) return null;
  const { slots: _s, primary: _p, ...top } = model;
  return top as ModelSlot;
}

/** 需求 1 的便捷判定：该引擎是否仍在装载/排队（用于"模型加载中"文案）。 */
export function isLoadingSlot(slot: ModelSlot | null | undefined): boolean {
  if (!slot) return false;
  const state = slot.state;
  return state === 'loading' || state === 'queued' || state === 'pending';
}

/** 需求 4：`detail` 的分段展示口径（顺序即因果顺序，null 段表示该档被跳过）。 */
export const LOAD_DETAIL_SEGMENTS: { key: keyof ModelLoadDetail; labelKey: string }[] = [
  { key: 'import_s', labelKey: 'voices.segImport' },
  { key: 'weights_s', labelKey: 'voices.segWeights' },
  { key: 'compile_s', labelKey: 'voices.segCompile' },
  { key: 'warmup_s', labelKey: 'voices.segWarmup' },
  { key: 'restore_voices_s', labelKey: 'voices.segVoices' },
];

/**
 * 需求 4：把 detail 摊成"分段条"数据。
 *
 * 注意：`load_seconds`（旧键，含排队/调度）与 `detail.total_s`（只含加载本体）
 * 同源但口径不同，**两者相减不是排队时间**（US-C6 §风险 2 明确禁止），
 * 所以这里只并列展示，不做差值。
 */
export function detailSegments(detail: ModelLoadDetail | null | undefined):
  { key: string; seconds: number; ratio: number }[] {
  if (!detail) return [];
  const total = Number(detail.total_s);
  const base = Number.isFinite(total) && total > 0 ? total : 0;
  return LOAD_DETAIL_SEGMENTS
    .map(({ key, labelKey }) => {
      const raw = detail[key];
      const seconds = Number(raw);
      if (raw === null || raw === undefined || !Number.isFinite(seconds)) return null;
      return {
        key: labelKey,
        seconds,
        ratio: base > 0 ? Math.max(0, Math.min(1, seconds / base)) : 0,
      };
    })
    .filter((x): x is { key: string; seconds: number; ratio: number } => x !== null);
}

/**
 * 需求 2：seed 的调用纪律 —— **白名单**而非黑名单。
 *
 * 真兑现 seed 的引擎（后端 `supports_seed = True`）：
 *   voxcpm (modules/tts_engine/engines/voxcpm_adapter.py:59)、
 *   openvoice (modules/tts_engine/engines/openvoice_adapter.py:147)。
 * 其余（kokoro/edgetts/sample/gptsovits/index_tts/moss_tts）声明 False，
 * 收到非空 seed 会**显式 400**（base_tts.py:32,50 `_reject_seed`）。
 * 未知引擎名按"不支持"处理 —— 宁可不发，也不要 400。
 */
export const SEED_HONORING_ENGINES: ReadonlySet<string> = new Set(['voxcpm', 'openvoice']);

export function supportsSeed(engine: string | null | undefined): boolean {
  return !!engine && SEED_HONORING_ENGINES.has(engine);
}

/** 唯一的 seed 出口：门不过时返回 undefined，序列化后**请求体里连键都没有**。 */
export function seedForRequest(engine: string | null | undefined,
                               raw: string | number | null | undefined): number | undefined {
  if (!supportsSeed(engine)) return undefined;
  if (raw === null || raw === undefined || raw === '') return undefined;
  const n = typeof raw === 'number' ? raw : Number(String(raw).trim());
  return Number.isInteger(n) && n >= 0 ? n : undefined;
}

export function readSeedHeader(res: Response): number | null {
  const raw = res.headers.get(SEED_HEADER);
  if (raw === null || raw.trim() === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/** 试听"嗓子归属"响应头（`backend/api/voices.py:234`、`cast.py:358` 同一键名）。 */
export const VOICE_BINDING_HEADER = 'X-Preview-Voice-Binding';

/** 这两个取值 = 听到的就是该音色声明的嗓子，不需要出警示徽标。 */
export const OWN_VOICE_BINDINGS: ReadonlySet<string> = new Set(['own_engine', 'native']);

export function readVoiceBindingHeader(res: Response): string | null {
  const raw = res.headers.get(VOICE_BINDING_HEADER);
  if (raw === null || raw.trim() === '') return null;
  return raw.trim();
}

/** 要不要向用户明说"这不是那枚音色原本的嗓子"。 */
export function isBorrowedVoice(binding: string | null | undefined): boolean {
  return !!binding && !OWN_VOICE_BINDINGS.has(binding);
}

/** 503 detail 里带回 `model_status`（backend/api/cast.py:258-268）时，取排队位次。 */
function queueHintFromDetail(detail: unknown): ModelSlot | null {
  const ms = (detail as any)?.model_status;
  if (!ms || typeof ms !== 'object') return null;
  return ms as ModelSlot;
}

/** 试听走 fetch（需要读响应头 / 拿到 503 的结构体 detail），故与 axios 分开实现。 */
export async function fetchVoicePreview(voiceId: string): Promise<PreviewResult> {
  const res = await fetch(`/api/voices/${encodeURIComponent(voiceId)}/preview`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new PreviewUnavailableError(
      detailText(err?.detail, `试听不可用 (${res.status})`),
      queueHintFromDetail(err?.detail),
    );
  }
  return {
    url: URL.createObjectURL(await res.blob()),
    seed: readSeedHeader(res),
    voiceBinding: readVoiceBindingHeader(res),
  };
}

// ========== 混音设置 API（project.global_settings.mix） ==========
export const mixApi = {
  get: (id: string) => api.get(`/dubbing/projects/${id}/mix-settings`),
  update: (id: string, mix: MixSettings | null) =>
    api.put(`/dubbing/projects/${id}/mix-settings`, { mix }),
};

// ========== BGM / 音效素材库 API ==========
export const bgmApi = {
  list: (mood?: string) => api.get('/bgm', { params: mood ? { mood } : undefined }),
  upload: (file: File, opts: { name?: string; kind?: string; moods?: string[]; loopable?: boolean } = {}) => {
    const form = new FormData();
    form.append('file', file);
    if (opts.name) form.append('name', opts.name);
    form.append('kind', opts.kind || 'bgm');
    if (opts.moods?.length) form.append('moods', opts.moods.join(','));
    if (opts.loopable !== undefined) form.append('loopable', String(opts.loopable));
    return api.post('/bgm/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    });
  },
  previewUrl: (bgmId: string) => `/api/bgm/${encodeURIComponent(bgmId)}/preview`,
};

// ========== 任务 API ==========
export const taskApi = {
  startConvert: (data: any) => api.post('/tasks/convert', data),
  list: () => api.get('/tasks/'),
  get: (id: string) => api.get(`/tasks/${id}`),
  progress: (id: string) => api.get(`/tasks/${id}/progress`),
  pause: (id: string) => api.post(`/tasks/${id}/pause`),
  resume: (id: string) => api.post(`/tasks/${id}/resume`),
  cancel: (id: string) => api.post(`/tasks/${id}/cancel`),
  delete: (id: string) => api.delete(`/tasks/${id}`),
};

// ========== 系统 API ==========
// （systemApi 已随 L14 接口消费包上移到 castApi 之后，含 modelStatus）

export default api;