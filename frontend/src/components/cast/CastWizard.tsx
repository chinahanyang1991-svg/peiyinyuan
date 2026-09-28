/**
 * CastWizard —— 多人有声书向导（批次 B 前端主入口）
 *
 * 五步（spec 7）:
 *   1 选书            -> 选一本已导入的书，建/切到多人配音项目
 *   2 角色分析        -> POST analyze-cast，WS /api/tasks/{id}/progress 实时进度 + 失败重试（WS 断了自动 HTTP 轮询）
 *   3 角色卡网格      -> 属性徽章 + 推荐音色置顶（角色建议获取）+ 试听 + 上传自定义，PUT cast/confirm 落库（确认）
 *   4 音乐与混音      -> MixPanel（BGM/闪避/角色增益/主增益/LUFS），PUT mix-settings
 *   5 生成            -> 摘要 + 开始转换（沿用父级 taskApi.startConvert）
 *
 * 契约要点（重制时未改动）：
 *  - 角色建议：GET cast/suggestions?top=5 仅在 cast.analyzed 时拉取，按 role_name 归组传给角色卡；
 *  - 确认落库：PUT cast/confirm { roles[], auto_assign_rest }，dirty 未落库离步要二次确认；
 *  - 旁白标记：cast.narrator 优先，回落 role_type === 'narrator' 的角色名，第 5 步摘要与角色卡都要显式标出；
 *  - 音色五类：可选音色列表按 human/animal/electronic/anime/mechanical 过滤（RoleCardGrid 内实现）。
 *
 * 与后端的契约见 backend/api/cast.py；类型见 services/api.ts 的 CastSnapshot/CastRole/MixSettings。
 * 长列表（角色卡、书目、增益表）一律走 VirtualList，避免大书卡顿。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Ban, Check, Play, RefreshCw, Sparkles } from "lucide-react";
import { VoiceBindingBadge } from "../ModelStatusBar";
import { bgmApi, castApi, detailText, dubbingApi, mixApi, taskApi } from "../../services/api";
import type { BgmItem, CastRole, CastSnapshot, MixSettings, VoiceSuggestion } from "../../services/api";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useI18n } from "../../i18n";
import type { MessageKey } from "../../i18n";
import {
  Alert,
  Badge,
  BadgeStack,
  Button,
  ButtonGroup,
  Card,
  DetailList,
  EmptyState,
  ProgressBar,
  StatusBadge,
  Steps,
  useConfirm,
  useToast,
  WizardNav,
} from "../ui";
import VirtualList from "./VirtualList";
import RoleCardGrid from "./RoleCardGrid";
import type { CastVoice } from "./RoleCardGrid";
import MixPanel from "./MixPanel";

export interface WizardBook {
  id: string;
  title: string;
  author?: string;
  format?: string;
  total_chars?: number;
  language?: string;
  chapter_count?: number;
}

interface CastWizardProps {
  projectId: string;
  projectName: string;
  bookId: string | null;
  books: WizardBook[];
  voices: CastVoice[];
  globalSettings: { speed: number; pitch: number; emotion: string };
  onSelectProject?: (id: string) => void;
  onBookChosen?: (bookId: string) => void;
  onConvert: () => void;
  converting?: boolean;
}

const TERMINAL = ["completed", "failed", "cancelled"];
/** 兜底轮询连续失败上限（1.5s 一跳 = 约 15s）：到点就停下并报出来，不许让进度条永远卡住 */
const POLL_FAIL_LIMIT = 10;
const STEP_KEYS: MessageKey[] = [
  "wizard.step1",
  "wizard.step2",
  "wizard.step3",
  "wizard.step4",
  "wizard.step5",
];
function errMsg(err: any, fallback: string): string {
  // detail 可能是后端返回的结构体，扁平化后再上屏，否则会渲染成 "[object Object]"
  return detailText(err?.response?.data?.detail) || err?.message || fallback;
}

export default function CastWizard({
  projectId,
  projectName,
  bookId,
  books,
  voices,
  globalSettings,
  onSelectProject,
  onBookChosen,
  onConvert,
  converting,
}: CastWizardProps) {
  const { t, tEnum, format } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();

  const [step, setStep] = useState(1);
  const [cast, setCast] = useState<CastSnapshot | null>(null);
  const [draftRoles, setDraftRoles] = useState<CastRole[]>([]);
  const [suggestions, setSuggestions] = useState<Record<string, VoiceSuggestion[]>>({});
  const [mix, setMix] = useState<MixSettings>({});
  const [mixPassthrough, setMixPassthrough] = useState(true);
  const [bgmItems, setBgmItems] = useState<BgmItem[]>([]);
  const [sfxItems, setSfxItems] = useState<BgmItem[]>([]);
  const [ffmpegAvailable, setFfmpegAvailable] = useState<boolean | undefined>(undefined);
  const [librarySr, setLibrarySr] = useState<number | undefined>(undefined);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskState, setTaskState] = useState<{
    status: string;
    progress: number;
    stage: string;
    message?: string;
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [dirty, setDirty] = useState(false);
  const [previewingId, setPreviewingId] = useState<string | null>(null);
  const [previewBinding, setPreviewBinding] = useState<string | null>(null);
  const [uploadingId, setUploadingId] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // ---------- 数据加载 ----------
  const loadCast = useCallback(
    async (silent = false) => {
      if (!projectId) return;
      try {
        const res = await castApi.get(projectId);
        const data = res.data;
        setCast(data);
        setDraftRoles(data.roles || []);
        setDirty(false);
        // silent 只压制报错横幅；推荐音色只要 analyzed 就必须拉，否则冷启动第 3 步永远没有置顶组
        if (data.analyzed) {
          const sres = await castApi.suggestions(projectId, 5);
          const byRole: Record<string, VoiceSuggestion[]> = {};
          (sres.data.roles || []).forEach((r: any) => {
            byRole[r.role_name] = r.suggestions || [];
          });
          setSuggestions(byRole);
        }
      } catch (err: any) {
        if (!silent) setError(errMsg(err, t("wizard.readCastFailed")));
      }
    },
    [projectId, t]
  );

  useEffect(() => {
    setError("");
    setTaskId(null);
    setTaskState(null);
    setSuggestions({});
    loadCast(true);
  }, [projectId, loadCast]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await mixApi.get(projectId);
        if (!alive) return;
        setMix(res.data.mix || {});
        setMixPassthrough(Boolean(res.data.passthrough));
      } catch (err) {
        /* 老项目没有 mix 也正常：保持空配置直通 */
        console.warn("读取 mix 配置失败，按直通处理:", err);
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await bgmApi.list();
        if (!alive) return;
        setBgmItems(res.data.bgm || []);
        setSfxItems([...(res.data.sfx || []), ...(res.data.external_sfx || [])]);
        setFfmpegAvailable(Boolean(res.data.ffmpeg_available));
        setLibrarySr(res.data.library_sr);
      } catch (err) {
        /* 素材库读不到时面板留空，不阻塞向导 */
        console.warn("读取 BGM/SFX 素材库失败:", err);
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  // ---------- 任务进度（WS + HTTP 兜底） ----------
  const { data: ws, connected } = useWebSocket(taskId || undefined, {
    enabled: !!taskId && !TERMINAL.includes(taskState?.status || ""),
  });

  useEffect(() => {
    if (!ws) return;
    setTaskState({
      status: ws.status,
      progress: Number(ws.progress || 0),
      stage: ws.current_stage || "",
      message: ws.error_message || undefined,
    });
    if (TERMINAL.includes(ws.status)) {
      if (ws.status === "completed") {
        loadCast();
        toast.success(t("wizard.analysisOk"));
        setStep(3);
      } else {
        setError(
          ws.error_message ||
            (ws.status === "cancelled" ? t("wizard.analysisCancelled") : t("wizard.analysisFailed"))
        );
      }
    }
  }, [ws, loadCast, toast, t]);

  // WS 不可用时用 HTTP 轮询顶上（同一路由，进度也写在 DB 里）
  useEffect(() => {
    if (!taskId || connected) return;
    if (taskState && TERMINAL.includes(taskState.status)) return;
    let fails = 0;
    const timer = window.setInterval(async () => {
      try {
        const res = await taskApi.progress(taskId);
        const p = res.data;
        fails = 0;
        setTaskState({
          status: p.status,
          progress: Number(p.progress || 0),
          stage: p.current_stage || "",
          message: p.error_message || undefined,
        });
        if (TERMINAL.includes(p.status)) {
          window.clearInterval(timer);
          if (p.status === "completed") {
            loadCast();
            setStep(3);
          }
        }
      } catch (err) {
        fails += 1;
        console.error("轮询任务进度失败:", err);
        if (fails >= POLL_FAIL_LIMIT) {
          // 一直失败却什么都不说，等于让用户盯着一根卡住的进度条
          window.clearInterval(timer);
          setError(t("wizard.pollGone"));
        }
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [taskId, connected, taskState, loadCast, setError, t]);

  // ---------- 步骤动作 ----------
  const startAnalysis = useCallback(
    async (useLlm: boolean) => {
      if (!projectId) return;
      setBusy("analyze");
      setError("");
      try {
        const res = await castApi.analyze(projectId, { use_llm: useLlm });
        const task = res.data.task;
        setTaskId(task.id);
        setTaskState({ status: task.status || "pending", progress: 0, stage: "queued" });
      } catch (err: any) {
        setError(errMsg(err, t("wizard.startAnalysisFailed")));
      }
      setBusy("");
    },
    [projectId, t]
  );

  const cancelAnalysis = useCallback(async () => {
    if (!taskId) return;
    try {
      await taskApi.cancel(taskId);
      toast.info(t("wizard.cancelRequested"));
    } catch (err: any) {
      setError(errMsg(err, t("wizard.cancelFailed")));
    }
  }, [taskId, toast, t]);

  const patchRole = useCallback((role: CastRole, part: Record<string, any>) => {
    setDraftRoles((prev) =>
      prev.map((r) => {
        if (r.id !== role.id) return r;
        const next: CastRole = { ...r, ...part };
        if (part.mix) next.mix = { ...(r.mix || {}), ...part.mix };
        return next;
      })
    );
    setDirty(true);
  }, []);

  const patchRoleMix = useCallback(
    (role: CastRole, nextMix: { gain_db?: number; speed?: number; pitch?: number }) => {
      patchRole(role, { mix: nextMix });
    },
    [patchRole]
  );

  const confirmRoles = useCallback(async () => {
    if (!projectId) return;
    setBusy("confirm");
    setError("");
    try {
      const payload = draftRoles.map((r) => ({
        role_id: r.id,
        name: r.name,
        role_type: r.role_type,
        voice_id: r.voice_id || null,
        aliases: r.aliases || [],
        gender: r.cast?.gender,
        age_group: r.cast?.age_group,
        identity: r.cast?.identity,
        personality_tags: r.cast?.personality_tags || [],
        mix: r.mix || {},
      }));
      await castApi.confirm(projectId, { roles: payload, auto_assign_rest: false });
      setDirty(false);
      await loadCast(true);
      toast.success(t("wizard.confirmOk"));
    } catch (err: any) {
      setError(errMsg(err, t("wizard.confirmFailed")));
    }
    setBusy("");
  }, [draftRoles, loadCast, projectId, toast, t]);

  const autoAssign = useCallback(async () => {
    if (!projectId) return;
    setBusy("auto");
    setError("");
    try {
      await castApi.confirm(projectId, { roles: [], auto_assign_rest: true });
      await loadCast();
      toast.success(t("wizard.autoAssignOk"));
    } catch (err: any) {
      setError(errMsg(err, t("wizard.autoAssignFailed")));
    }
    setBusy("");
  }, [loadCast, projectId, toast, t]);

  const saveMix = useCallback(
    async (next: MixSettings) => {
      if (!projectId) return;
      setBusy("mix");
      setError("");
      try {
        const res = await mixApi.update(projectId, next);
        setMix(res.data.mix || next);
        setMixPassthrough(Boolean(res.data.passthrough));
        toast.success(t("mix.saveOk"));
      } catch (err: any) {
        setError(errMsg(err, t("mix.saveFailed")));
      }
      setBusy("");
    },
    [projectId, t, toast]
  );

  const uploadBgm = useCallback(
    async (file: File) => {
      setBusy("bgm");
      setError("");
      try {
        await bgmApi.upload(file, { name: file.name.replace(/\.[^.]+$/, "") });
        const res = await bgmApi.list();
        setBgmItems(res.data.bgm || []);
        setSfxItems([...(res.data.sfx || []), ...(res.data.external_sfx || [])]);
        setFfmpegAvailable(Boolean(res.data.ffmpeg_available));
        toast.success(t("mix.uploadOk", { name: file.name }));
      } catch (err: any) {
        setError(errMsg(err, t("mix.uploadFailed")));
      }
      setBusy("");
    },
    [t, toast]
  );

  const previewRole = useCallback(
    async (role: CastRole) => {
      if (!projectId) return;
      setPreviewingId(role.id);
      setError("");
      setPreviewBinding(null);
      try {
        const res = await castApi.previewWithMeta(projectId, { role_id: role.id, max_sentences: 2 });
        setPreviewBinding(res.voiceBinding);
        if (!audioRef.current) audioRef.current = new Audio();
        audioRef.current.src = res.url;
        await audioRef.current.play().catch(() => undefined);
        URL.revokeObjectURL(res.url);
      } catch (err: any) {
        setError(errMsg(err, t("roles.previewFailed")));
      }
      setPreviewingId(null);
    },
    [projectId, t]
  );

  const createProjectFromBook = useCallback(
    async (book: WizardBook) => {
      setBusy("create");
      setError("");
      try {
        const res = await dubbingApi.createProject({
          name: `${book.title} · ${tEnum("mode", "multi")}`,
          book_id: book.id,
          mode: "multi",
        });
        const p = res.data.project || res.data;
        onBookChosen?.(book.id);
        if (p?.id && onSelectProject) onSelectProject(p.id);
        toast.success(t("dubbing.createOk"));
      } catch (err: any) {
        setError(errMsg(err, t("dubbing.createFailed")));
      }
      setBusy("");
    },
    [onBookChosen, onSelectProject, t, toast]
  );

  // ---------- 派生 ----------
  const assigned = useMemo(() => draftRoles.filter((r) => r.voice_id).length, [draftRoles]);
  const unknownAttrs = useMemo(
    () =>
      draftRoles.filter((r) =>
        ["gender", "age_group", "identity"].some((k) => (r.cast as any)?.[k] === "unknown")
      ).length,
    [draftRoles]
  );
  const narrator = cast?.narrator || draftRoles.find((r) => r.role_type === "narrator")?.name || null;
  const currentBook = useMemo(() => books.find((b) => b.id === bookId) || null, [books, bookId]);
  const running = !!taskId && !!taskState && !TERMINAL.includes(taskState.status);
  const canLeaveStep2 = !!cast?.analyzed || draftRoles.length > 0;
  const stageText = (() => {
    const stage = taskState?.stage || "";
    if (!stage) return cast?.analyzed ? t("wizard.alreadyAnalyzed") : t("wizard.notStarted");
    return tEnum("stage", stage, stage);
  })();

  const goStep = async (n: number) => {
    if (n === step) return;
    if (n > step && step === 3 && dirty) {
      const ok = await confirm({ title: t("common.confirmLeaveTitle"), message: t("wizard.leaveDirty") });
      if (!ok) return;
    }
    setStep(n);
  };

  const mixSummaryText = t("wizard.mixSummary", {
    bgm: mix.bgm?.ref || t("common.none"),
    gain: t("mix.dB", { value: format.decimal(mix.master?.gain_db ?? 0, 1) }),
    lufs: mix.master?.target_lufs != null ? String(mix.master.target_lufs) : t("wizard.lufsUnset"),
  });

  return (
    <div className="wizard">
      <Steps
        items={STEP_KEYS.map((key, i) => ({ id: String(i + 1), label: t(key) }))}
        activeId={String(step)}
        maxReachable={step}
        onSelect={(_id, index) => goStep(index + 1)}
      />

      {error ? (
        <Alert
          tone="danger"
          action={
            step === 2 ? (
              <Button size="xs" variant="secondary" onClick={() => startAnalysis(true)}>
                {t("wizard.retryAnalysis")}
              </Button>
            ) : undefined
          }
          onDismiss={() => setError("")}
        >
          {error}
        </Alert>
      ) : null}

      {/* 1 选书 */}
      {step === 1 ? (
        <div className="wizard__body">
          <Card title={t("wizard.currentProject")} desc={projectName || t("wizard.noProject")}>
            {currentBook ? (
              <div className="u-col u-gap-1">
                <strong className="u-truncate">{currentBook.title}</strong>
                <span className="u-xs u-muted">
                  {t("wizard.bookMeta", {
                    author: currentBook.author || t("edit.anonymous"),
                    chapters: format.number(currentBook.chapter_count ?? 0),
                    chars: format.number(currentBook.total_chars ?? 0),
                    lang: tEnum("language", currentBook.language || "zh"),
                  })}
                </span>
              </div>
            ) : (
              <p className="hint">{t("wizard.noBookInProject")}</p>
            )}
          </Card>

          <Card
            title={t("wizard.libraryTitle")}
            desc={t("wizard.libraryHint")}
            actions={<Badge tone="neutral">{t("wizard.libraryCount", { count: books.length })}</Badge>}
          >
            <VirtualList
              className="pick-list"
              items={books}
              rowHeight={56}
              columns={1}
              height={280}
              keyOf={(b: WizardBook) => b.id}
              empty={<EmptyState title={t("wizard.libraryEmpty")} />}
              renderItem={(b: WizardBook) => (
                <div className="pick-row" data-active={b.id === bookId ? "true" : "false"}>
                  <span className="pick-row__body">
                    <span className="pick-row__title">{b.title}</span>
                    <span className="u-xs u-muted">
                      {t("wizard.bookMeta", {
                        author: b.author || t("edit.anonymous"),
                        chapters: format.number(b.chapter_count ?? 0),
                        chars: format.number(b.total_chars ?? 0),
                        lang: tEnum("language", b.language || "zh"),
                      })}
                    </span>
                  </span>
                  {b.id === bookId ? (
                    <Badge tone="info">{t("wizard.currentTag")}</Badge>
                  ) : (
                    <Button
                      size="xs"
                      variant="secondary"
                      loading={busy === "create"}
                      onClick={() => createProjectFromBook(b)}
                    >
                      {t("wizard.useThisBook")}
                    </Button>
                  )}
                </div>
              )}
            />
          </Card>

          <WizardNav>
            <Button variant="primary" onClick={() => goStep(2)}>
              {t("wizard.nextToAnalysis")}
            </Button>
          </WizardNav>
        </div>
      ) : null}

      {/* 2 角色分析 */}
      {step === 2 ? (
        <div className="wizard__body">
          <Card
            title={t("wizard.analysisTitle")}
            desc={t("wizard.analysisDesc")}
            actions={
              <Badge tone={connected ? "success" : "warning"} dot>
                {connected ? t("wizard.realtimePush") : t("wizard.httpPolling")}
              </Badge>
            }
          >
            <ProgressBar
              value={Math.min(100, Math.round((taskState?.progress || 0) * 100))}
              label={stageText}
              valueText={
                taskState ? `${Math.round((taskState.progress || 0) * 100)}%` : cast?.analyzed ? "100%" : "—"
              }
              tone={taskState?.status === "failed" ? "danger" : "accent"}
              indeterminate={running && !taskState?.progress}
            />
            <div className="u-row u-center u-wrap u-gap-2">
              <StatusBadge status={taskState?.status || "pending"} fallback={t("wizard.notStarted")} />
              {taskState?.message ? <span className="u-xs u-muted u-truncate">{taskState.message}</span> : null}
            </div>

            <ButtonGroup>
              <Button
                variant="primary"
                icon={Sparkles}
                loading={busy === "analyze"}
                disabled={running}
                onClick={() => startAnalysis(true)}
              >
                {running
                  ? t("wizard.analyzing")
                  : cast?.analyzed
                    ? t("wizard.restartLLM")
                    : t("wizard.startLLM")}
              </Button>
              <Button variant="secondary" disabled={busy === "analyze" || running} onClick={() => startAnalysis(false)}>
                {t("wizard.startRules")}
              </Button>
              {running ? (
                <Button variant="ghost" icon={Ban} onClick={cancelAnalysis}>
                  {t("common.cancel")}
                </Button>
              ) : null}
              {cast?.analyzed && !running ? (
                <Button variant="ghost" icon={RefreshCw} onClick={() => loadCast()}>
                  {t("wizard.refreshCast")}
                </Button>
              ) : null}
            </ButtonGroup>

            {cast?.analyzed ? (
              <BadgeStack>
                <Badge tone="neutral">{t("wizard.statRoles", { count: cast.role_count ?? 0 })}</Badge>
                <Badge tone="neutral">
                  {t("wizard.statCharacters", { count: cast.character_count ?? 0 })}
                </Badge>
                <Badge tone={narrator ? "info" : "warning"}>
                  {narrator ? t("wizard.statNarrator", { name: narrator }) : t("wizard.statNarratorNone")}
                </Badge>
                {cast.cast?.stats?.chars != null ? (
                  <Badge tone="neutral">
                    {t("wizard.statChars", { count: format.number(Number(cast.cast.stats.chars)) })}
                  </Badge>
                ) : null}
                {cast.cast?.stats?.chapters != null ? (
                  <Badge tone="neutral">{t("wizard.statChapters", { count: cast.cast.stats.chapters })}</Badge>
                ) : null}
                {cast.cast?.stats?.llm != null ? (
                  <Badge tone={cast.cast.stats.llm === "llm" ? "accent" : "warning"}>
                    {t("wizard.statProfile", {
                      source: tEnum("profileSource", String(cast.cast.stats.llm) === "llm" ? "llm" : "rules"),
                    })}
                  </Badge>
                ) : null}
                {cast.cast?.stats?.llm_reason ? (
                  <Badge tone="warning" title={String(cast.cast.stats.llm_reason)}>
                    {t("wizard.statProfileReason", { reason: String(cast.cast.stats.llm_reason) })}
                  </Badge>
                ) : null}
                {cast.cast?.stats?.elapsed_seconds != null ? (
                  <Badge tone="neutral" className="u-num">
                    {t("wizard.statElapsed", { seconds: Number(cast.cast.stats.elapsed_seconds).toFixed(2) })}
                  </Badge>
                ) : null}
                {cast.cast?.status ? (
                  <Badge tone="neutral">
                    {t("wizard.analyzedAt", { time: format.dateTime(String(cast.cast.status)) })}
                  </Badge>
                ) : null}
              </BadgeStack>
            ) : null}
          </Card>

          <WizardNav
            left={
              <Button variant="secondary" onClick={() => goStep(1)}>
                {t("common.previous")}
              </Button>
            }
          >
            <Button variant="primary" onClick={() => goStep(3)} disabled={!canLeaveStep2}>
              {t("wizard.nextToAssign")}
            </Button>
          </WizardNav>
        </div>
      ) : null}

      {/* 3 角色分配 */}
      {step === 3 ? (
        <div className="wizard__body">
          <Card
            title={t("wizard.assignmentTitle")}
            desc={t("wizard.assignmentHint")}
            actions={
              <BadgeStack>
                <Badge tone="accent">
                  {t("wizard.assignedProgress", { assigned, total: draftRoles.length })}
                </Badge>
                {unknownAttrs > 0 ? (
                  <Badge tone="warning">{t("wizard.pendingAttrs", { count: unknownAttrs })}</Badge>
                ) : null}
                {dirty ? <Badge tone="info">{t("common.unsavedChanges")}</Badge> : null}
                <VoiceBindingBadge binding={previewBinding} />
              </BadgeStack>
            }
          >
            <RoleCardGrid
              roles={draftRoles}
              suggestions={suggestions}
              voices={voices}
              onChange={patchRole}
              onPreview={previewRole}
              previewingId={previewingId}
              uploadingId={uploadingId}
              onUploaded={(message) => toast.info(message)}
              height={Math.max(360, Math.min(560, draftRoles.length ? 520 : 200))}
            />
            <div className="wizard__section">
              <ButtonGroup>
                <Button
                  variant="primary"
                  icon={Check}
                  loading={busy === "confirm"}
                  disabled={busy === "confirm"}
                  onClick={confirmRoles}
                >
                  {busy === "confirm" ? t("wizard.confirming") : t("wizard.confirmRoles")}
                </Button>
                <Button variant="secondary" disabled={busy === "auto"} onClick={autoAssign}>
                  {t("wizard.autoAssign")}
                </Button>
              </ButtonGroup>
              <p className="hint">{t("wizard.autoAssignHint")}</p>
            </div>
          </Card>

          <WizardNav
            left={
              <Button variant="secondary" onClick={() => goStep(2)}>
                {t("common.previous")}
              </Button>
            }
          >
            <Button variant="primary" onClick={() => goStep(4)} disabled={assigned === 0}>
              {t("wizard.nextToMix")}
            </Button>
          </WizardNav>
        </div>
      ) : null}

      {/* 4 音乐与混音 */}
      {step === 4 ? (
        <div className="wizard__body">
          <Card
            title={t("wizard.mixTitle")}
            actions={
              <Badge tone={mixPassthrough ? "neutral" : "success"}>
                {mixPassthrough ? t("mix.passthroughOn") : t("mix.passthroughOff")}
              </Badge>
            }
          >
            <MixPanel
              mix={mix}
              roles={draftRoles}
              bgmItems={bgmItems}
              sfxItems={sfxItems}
              ffmpegAvailable={ffmpegAvailable}
              librarySr={librarySr}
              passthrough={mixPassthrough}
              onChange={setMix}
              onRoleMix={patchRoleMix}
              onUploadBgm={uploadBgm}
              height={300}
            />
            <ButtonGroup>
              <Button
                variant="primary"
                loading={busy === "mix"}
                disabled={busy === "bgm" || busy === "mix"}
                onClick={() => saveMix(mix)}
              >
                {busy === "mix" ? t("mix.saving") : t("mix.saveBtn")}
              </Button>
              {busy === "bgm" ? <span className="hint">{t("mix.uploadWorking")}</span> : null}
            </ButtonGroup>
          </Card>

          <WizardNav
            left={
              <Button variant="secondary" onClick={() => goStep(3)}>
                {t("common.previous")}
              </Button>
            }
          >
            <Button variant="primary" onClick={() => goStep(5)}>
              {t("wizard.nextToGenerate")}
            </Button>
          </WizardNav>
        </div>
      ) : null}

      {/* 5 生成 */}
      {step === 5 ? (
        <div className="wizard__body">
          <Card title={t("wizard.summaryTitle")}>
            <DetailList
              items={[
                { label: t("wizard.summaryProject"), value: projectName },
                { label: t("wizard.summaryBook"), value: currentBook?.title || bookId || "—" },
                {
                  label: t("wizard.summaryRoles"),
                  value: t("wizard.summaryRolesValue", { total: draftRoles.length, assigned }),
                },
                {
                  label: t("wizard.summaryNarrator"),
                  value: narrator || t("wizard.summaryNarratorFallback"),
                },
                { label: t("wizard.summaryMix"), value: mixPassthrough ? t("wizard.summaryMixPassthrough") : mixSummaryText },
                {
                  label: t("wizard.summaryGlobals"),
                  value: t("wizard.summaryGlobalsValue", {
                    speed: `${globalSettings.speed}×`,
                    pitch: globalSettings.pitch,
                    emotion: tEnum("emotion", globalSettings.emotion),
                  }),
                },
                {
                  label: t("wizard.summaryAliases"),
                  value: t("wizard.summaryAliasesValue", {
                    count: Object.keys(cast?.cast?.aliases || {}).length,
                  }),
                },
              ]}
            />
            {assigned === 0 ? <Alert tone="warning">{t("wizard.noVoiceWarn")}</Alert> : null}
          </Card>

          <WizardNav
            left={
              <Button variant="secondary" onClick={() => goStep(4)}>
                {t("common.previous")}
              </Button>
            }
          >
            <Button variant="primary" icon={Play} loading={converting} disabled={converting} onClick={onConvert}>
              {converting ? t("wizard.convertWorking") : t("wizard.convertBtn")}
            </Button>
          </WizardNav>
        </div>
      ) : null}

      <audio ref={audioRef} preload="none" />
    </div>
  );
}
