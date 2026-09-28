/**
 * 配音编排页（/dubbing、/dubbing/new、/dubbing/:projectId）
 *
 * 后端契约（与重制前逐条对齐，端点/方法/载荷语义均未改动）：
 *  - GET    /api/dubbing/projects                          项目列表
 *  - GET    /api/dubbing/projects/{id}                     项目详情（含 roles/assignments）
 *  - POST   /api/dubbing/projects                          { name, book_id, mode }
 *  - GET    /api/books | /api/books/{id} | /api/books/{id}/chapters
 *  - GET    /api/voices?limit=&offset=                     音色列表（按 total 翻页取满整库，UI-100 L1）
 *  - POST   /api/dubbing/projects/{id}/roles               { name, role_type }
 *  - DELETE /api/dubbing/projects/{id}/roles/{roleId}
 *  - PUT    /api/dubbing/projects/{id}/roles/assign-voice  { role_id, voice_id }
 *  - PUT    /api/dubbing/projects/{id}/assignments         { assignments }
 *  - PUT    /api/dubbing/projects/{id}/settings            { settings }
 *  - POST   /api/dubbing/projects/{id}/preview             -> audio blob（试听）
 *  - POST   /api/tasks/convert                             开始转换（taskApi.startConvert）
 *
 * PY-318：路由 /dubbing/new 的 "new" 是动作占位符，不是项目 id —— 绝不带它去请求详情（否则 404）。
 * multi 模式默认渲染 CastWizard（五步向导），三栏编辑器作为"高级编辑"保留。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { Clapperboard, Plus, Trash2, Wand2 } from "lucide-react";
import { bookApi, dubbingApi, taskApi, voiceApi } from "../services/api";
import { normalizeTier, type TtsTier } from "../store/appStore";
import { usePrefsStore } from "../store/prefsStore";
import TtsModeToggle from "../components/TtsModeToggle";
import { useI18n } from "../i18n";
import {
  Alert,
  Badge,
  Button,
  Card,
  DetailList,
  EmptyState,
  Field,
  IconButton,
  Input,
  LoaderBlock,
  Modal,
  Page,
  PageHeader,
  Segmented,
  Select,
  Slider,
  StatusBadge,
  TabPanel,
  TabPanels,
  Tabs,
  Toolbar,
  ToolbarSpacer,
  useConfirm,
  useToast,
} from "../components/ui";
import CastWizard from "../components/cast/CastWizard";

interface Project {
  id: string;
  name: string;
  book_id: string;
  mode: string;
  status: string;
  /** `tier` 后端会存任意历史值，故不声明成 TtsTier —— 读出来先过 normalizeTier */
  global_settings: { speed: number; pitch: number; emotion: string; tier?: string };
  roles?: Role[];
  assignments?: Assignment[];
  role_count: number;
}

interface Role {
  id: string;
  project_id: string;
  name: string;
  role_type: string;
  voice_id: string | null;
}

interface Assignment {
  id: string;
  chapter_id: string;
  voice_id: string;
  role_id: string;
  start_char: number;
  end_char: number;
  settings: any;
}

interface Chapter {
  id: string;
  title: string;
  index: number;
  char_count: number;
  content?: string;
  content_preview?: string;
}

interface Voice {
  id: string;
  name: string;
  source: string;
  engine: string;
  language: string;
  gender: string;
  age_group?: string;
  category?: string;
  style_tags: string[];
  description: string;
}

interface Book {
  id: string;
  title: string;
  author: string;
  format: string;
  total_chars: number;
  language?: string;
  chapter_count?: number;
}

const ROLE_TYPES = ["narrator", "hero", "supporting", "custom"] as const;
const MODES = ["single", "multi", "mixed"] as const;
const EMOTION_OPTIONS = ["neutral", "happy", "sad", "angry", "excited", "calm", "serious", "whisper"];

/** 段条配色：与音色无关，仅按角色序号取色（与重制前一致） */
const SEGMENT_COLORS = [
  "#6366f1", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
  "#ec4899", "#06b6d4", "#f97316", "#14b8a6", "#a855f7",
];

/** FastAPI 错误在 detail 里；长度不可控，只做类型收敛 */
function detailOf(err: unknown): string | null {
  const detail = (err as any)?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  const message = (err as Error)?.message;
  return message && message.trim() ? message : null;
}

export default function DubbingPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const bookIdParam = searchParams.get("bookId");
  const { t, tEnum, format } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();

  // ---------- 主数据 ----------
  const [projects, setProjects] = useState<Project[]>([]);
  // PY-318：/dubbing/new 的 "new" 是动作占位符而非项目 id，误当 id 请求会 404
  const initialProjectId = projectId && projectId !== "new" ? projectId : null;
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(initialProjectId);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [book, setBook] = useState<Book | null>(null);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [books, setBooks] = useState<Book[]>([]);

  // ---------- UI 状态 ----------
  const [loading, setLoading] = useState(true);
  const [projectLoading, setProjectLoading] = useState(false);
  const [selectedChapterIndex, setSelectedChapterIndex] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<"roles" | "assignments" | "settings">("roles");
  const prefsTtsTier = usePrefsStore((state) => state.ttsTier);
  /** 项目全局设置；`tier` = 指令#1 的配音模式（后端白名单在 dubbing_service.GLOBAL_SETTING_SPECS） */
  const [globalSettings, setGlobalSettings] = useState<{
    speed: number;
    pitch: number;
    emotion: string;
    tier: TtsTier;
  }>(() => ({ speed: 1.0, pitch: 0, emotion: "neutral", tier: prefsTtsTier }));
  const [settingsSaving, setSettingsSaving] = useState(false);

  // 新建项目弹窗
  const [showNewDialog, setShowNewDialog] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectBookId, setNewProjectBookId] = useState("");
  const [newProjectMode, setNewProjectMode] = useState("multi");
  const [creatingProject, setCreatingProject] = useState(false);

  // 添加角色
  const [newRoleName, setNewRoleName] = useState("");
  const [newRoleType, setNewRoleType] = useState("supporting");
  const [addingRole, setAddingRole] = useState(false);

  // 多人向导（批次 B）：multi 项目默认走五步向导，三栏编辑器为"高级编辑"
  const [wizardOpen, setWizardOpen] = useState(true);
  const [converting, setConverting] = useState(false);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // ---------- 派生 ----------
  const chapterAssignments = useCallback(
    (chapterId: string): Assignment[] => assignments.filter((a) => a.chapter_id === chapterId),
    [assignments]
  );
  const roleById = useCallback((roleId: string) => roles.find((r) => r.id === roleId), [roles]);
  const voiceById = useCallback((voiceId: string) => voices.find((v) => v.id === voiceId), [voices]);
  const roleColor = useCallback(
    (roleId: string) => {
      const idx = roles.findIndex((r) => r.id === roleId);
      return SEGMENT_COLORS[idx >= 0 ? idx % SEGMENT_COLORS.length : 0];
    },
    [roles]
  );
  const filteredVoices = book?.language
    ? voices.filter((v) => v.language === book.language || !v.language)
    : voices;
  const voiceOptions = filteredVoices.map((v) => ({
    value: v.id,
    label: `${v.name}（${tEnum("gender", v.gender, t("common.unknown"))} · ${v.engine || "—"}）`,
  }));
  /**
   * 已选音色不在当前列表（库还在加载 / 该音色已被删除）时补一条保留项：
   * 受控 `value` 若不落在任何 option 上，原生 select 会静默回落到 placeholder"未分配"，
   * 既骗人又会在下一次 change 时把真实 id 丢掉（与 RoleCardGrid 的 stale 兜底同款）。
   */
  const voiceOptionsFor = (picked?: string | null) => {
    if (!picked || voiceOptions.some((o) => o.value === picked)) return voiceOptions;
    return [
      ...voiceOptions,
      { value: picked, label: t("roles.voiceStale", { id: picked.slice(0, 8) }) },
    ];
  };
  const currentProjectSummary = projects.find((p) => p.id === selectedProjectId);

  const openNewDialog = useCallback(() => {
    bookApi.list().then((res) => setBooks(res.data.books || [])).catch((err) => console.error(err));
    setShowNewDialog(true);
  }, []);

  const closeNewDialog = useCallback(() => {
    setShowNewDialog(false);
    setNewProjectName("");
    setNewProjectBookId("");
    setNewProjectMode("multi");
  }, []);

  // ---------- 数据加载 ----------
  const loadProjects = useCallback(async () => {
    try {
      const res = await dubbingApi.listProjects();
      const list: Project[] = res.data.projects || res.data || [];
      setProjects(list);
      if (!selectedProjectId && list.length > 0) setSelectedProjectId(list[0].id);
    } catch (err) {
      console.error(err);
      toast.error(detailOf(err) ?? t("dubbing.loadFailed"));
    }
    setLoading(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast]);

  const loadBooks = useCallback(async () => {
    try {
      const res = await bookApi.list();
      setBooks(res.data.books || []);
    } catch (err) {
      console.error(err);
    }
  }, []);

  const loadProjectDetail = useCallback(async (id: string) => {
    setProjectLoading(true);
    try {
      const res = await dubbingApi.getProject(id);
      setSelectedProject(res.data.project || res.data);
    } catch (err) {
      console.error(err);
      toast.error(detailOf(err) ?? t("dubbing.loadFailed"));
    }
    setProjectLoading(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast]);

  const loadBookAndChapters = useCallback(async (bookId: string) => {
    try {
      const [bookRes, chaptersRes] = await Promise.all([bookApi.get(bookId), bookApi.getChapters(bookId)]);
      setBook(bookRes.data.book);
      setChapters(chaptersRes.data.chapters || []);
    } catch (err) {
      console.error(err);
    }
  }, []);

  /** 音色库拉取护栏：`inFlight` 去重并发翻页，`seq` 让后发起的请求覆盖先发者。 */
  const voicesFetch = useRef({ inFlight: false, seq: 0 });

  /**
   * 整张音色库一次取满（UI-100 L1）。
   * 旧实现只吃 `/voices/` 默认 limit=100 的那一页，而库里 517 条、后端 limit 上限 500，
   * 于是 4 个合法已分配音色（source=builtin / language=zh，都排在 100 名之后）被误标
   * "已分配但不在音色库"，且"全部音色"只有 100/517 可选。
   * 语言维度不再走后端二次拉取（那只会再截一刀并引入时序竞态），
   * 由下方 `filteredVoices` 在客户端按书语言收窄，读侧语义不变。
   */
  const loadVoices = useCallback(async () => {
    const gate = voicesFetch.current;
    if (gate.inFlight) return;
    gate.inFlight = true;
    const seq = ++gate.seq;
    try {
      const list = await voiceApi.listAll();
      if (seq === gate.seq) setVoices(list);
    } catch (err) {
      console.error(err);
    } finally {
      gate.inFlight = false;
    }
  }, []);

  const handleAutoCreate = useCallback(
    async (bkId: string) => {
      let bkTitle = "";
      try {
        const bkRes = await bookApi.get(bkId);
        bkTitle = bkRes.data.book?.title || "";
      } catch (err) {
        /* 书名取不到时用通用命名，不阻塞创建 */
        console.warn("读取书名失败，改用通用项目名:", err);
      }
      try {
        const res = await dubbingApi.createProject({
          name: bkTitle ? `${bkTitle} · ${t("dubbing.newProject")}` : t("dubbing.newProject"),
          book_id: bkId,
          mode: "multi",
        });
        const created = res.data.project || res.data;
        setProjects((prev) => [created, ...prev]);
        setSelectedProjectId(created.id);
        navigate(`/dubbing/${created.id}`, { replace: true });
        toast.success(t("dubbing.createOk"));
      } catch (err) {
        console.error(err);
        toast.error(detailOf(err) ?? t("dubbing.createFailed"));
      }
    },
    [navigate, t, toast]
  );

  // ---------- Effects ----------
  useEffect(() => {
    loadProjects();
    loadBooks();
  }, [loadProjects, loadBooks]);

  useEffect(() => {
    if (bookIdParam && projects.length > 0) {
      const existing = projects.find((p) => p.book_id === bookIdParam);
      if (existing) setSelectedProjectId(existing.id);
      else handleAutoCreate(bookIdParam);
    } else if (bookIdParam && projects.length === 0 && !loading) {
      handleAutoCreate(bookIdParam);
    }
  }, [bookIdParam, projects, loading, handleAutoCreate]);

  useEffect(() => {
    // PY-318：projectId === "new" 时不进详情分支，改为打开新建弹窗
    if (projectId === "new") {
      setSelectedProjectId(null);
      openNewDialog();
      return;
    }
    if (projectId) setSelectedProjectId(projectId);
  }, [projectId, openNewDialog]);

  useEffect(() => {
    if (selectedProjectId) loadProjectDetail(selectedProjectId);
  }, [selectedProjectId, loadProjectDetail]);

  useEffect(() => {
    if (!selectedProject) return;
    loadBookAndChapters(selectedProject.book_id);
    loadVoices();
    if (selectedProject.roles) setRoles(selectedProject.roles);
    if (selectedProject.assignments) setAssignments(selectedProject.assignments);
    if (selectedProject.global_settings) {
      setGlobalSettings({
        speed: selectedProject.global_settings.speed ?? 1.0,
        pitch: selectedProject.global_settings.pitch ?? 0,
        emotion: selectedProject.global_settings.emotion ?? "neutral",
        // 项目存过档位以项目为准；没存过（含脏值）沿用用户偏好，不猜
        tier: normalizeTier(selectedProject.global_settings.tier, prefsTtsTier),
      });
    }
  }, [selectedProject, loadBookAndChapters, loadVoices, prefsTtsTier]);

  /**
   * UI-100 L1：此处原本还会按 `book.language` 再拉一次后端筛选页（`loadVoices(language)`），
   * 两个拉取互相覆盖 `voices`，谁后落地决定"全部音色"是整库还是某一页 → 已分配音色是否被误标随
   * 加载时序漂移。现由上面的全量拉取单一来源喂数据，语言收窄交给 `filteredVoices`（客户端）。
   */

  // ---------- 项目操作 ----------
  const handleSelectProject = (id: string) => {
    if (!id) return;
    setSelectedProjectId(id);
    setSelectedChapterIndex(null);
    navigate(`/dubbing/${id}`, { replace: true });
  };

  const handleCreateProject = async () => {
    if (!newProjectName.trim() || !newProjectBookId) return;
    setCreatingProject(true);
    try {
      const res = await dubbingApi.createProject({
        name: newProjectName.trim(),
        book_id: newProjectBookId,
        mode: newProjectMode,
      });
      const created = res.data.project || res.data;
      setProjects((prev) => [created, ...prev]);
      setSelectedProjectId(created.id);
      navigate(`/dubbing/${created.id}`, { replace: true });
      closeNewDialog();
      toast.success(t("dubbing.createOk"));
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.createFailed"));
    }
    setCreatingProject(false);
  };

  // ---------- 试听与转换 ----------
  const handlePreview = async () => {
    if (!selectedProjectId) {
      toast.warning(t("dubbing.convertNeedProject"));
      return;
    }
    setPreviewPlaying(true);
    try {
      const response = await fetch(`/api/dubbing/projects/${selectedProjectId}/preview`, { method: "POST" });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        const detail =
          (errData as any)?.detail || t("dubbing.serverError", { status: String(response.status) });
        toast.error(t("dubbing.previewFailed") + " — " + String(detail));
        setPreviewPlaying(false);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      if (audioRef.current) {
        audioRef.current.src = url;
        await audioRef.current.play().catch(() => undefined);
      }
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.previewFailed") + " — " + t("common.networkError"));
    }
    setPreviewPlaying(false);
  };

  const handleStartConvert = async () => {
    if (!selectedProjectId || !selectedProject || converting) return;
    setConverting(true);
    try {
      const res = await taskApi.startConvert({
        project_id: selectedProjectId,
        book_id: selectedProject.book_id,
        output_format: "mp3",
        settings: globalSettings,
      });
      const task = res.data.task || res.data;
      navigate(`/convert/${task.id}`);
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.convertFailed"));
      setConverting(false);
    }
  };

  // ---------- 角色管理 ----------
  const handleAddRole = async () => {
    if (!newRoleName.trim() || !selectedProjectId) return;
    setAddingRole(true);
    try {
      const res = await dubbingApi.addRole(selectedProjectId, {
        name: newRoleName.trim(),
        role_type: newRoleType,
      });
      const created = res.data.role || res.data;
      setRoles((prev) => [...prev, created]);
      setNewRoleName("");
      setNewRoleType("supporting");
      toast.success(t("dubbing.roleAddOk"));
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.roleAddFailed"));
    }
    setAddingRole(false);
  };

  const handleDeleteRole = async (role: Role) => {
    if (!selectedProjectId) return;
    const ok = await confirm({
      title: t("common.confirmDeleteTitle"),
      message: t("dubbing.roleDeleteConfirm", { name: role.name }),
      tone: "danger",
    });
    if (!ok) return;
    try {
      await dubbingApi.deleteRole(selectedProjectId, role.id);
      setRoles((prev) => prev.filter((r) => r.id !== role.id));
      toast.success(t("dubbing.roleDeleteOk"));
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.roleDeleteFailed"));
    }
  };

  const handleAssignVoice = async (role: Role) => {
    if (!selectedProjectId) return;
    try {
      await dubbingApi.assignVoice(selectedProjectId, {
        role_id: role.id,
        voice_id: role.voice_id || "",
      });
      toast.success(t("dubbing.roleAssignOk"));
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.roleAssignFailed"));
    }
  };

  const handleVoiceSelectForRole = (roleId: string, voiceId: string) => {
    setRoles((prev) => prev.map((r) => (r.id === roleId ? { ...r, voice_id: voiceId || null } : r)));
  };

  const handleSaveSettings = async () => {
    if (!selectedProjectId) return;
    setSettingsSaving(true);
    try {
      await dubbingApi.updateSettings(selectedProjectId, globalSettings);
      toast.success(t("dubbing.settingsOk"));
    } catch (err) {
      toast.error(detailOf(err) ?? t("dubbing.settingsFailed"));
    }
    setSettingsSaving(false);
  };

  /**
   * 档位是"离散选择"，不像滑块那样需要用户再点一次保存：改完立刻落库。
   * 否则后端存的还是旧档 —— 同页的试听 (`POST /dubbing/projects/{id}/preview`)
   * 读的是**已保存的** global_settings，会出现"刚切了模式，试听却还是另一台引擎"。
   */
  const persistTier = (tier: TtsTier) => {
    const next = { ...globalSettings, tier };
    setGlobalSettings(next);
    if (!selectedProjectId) return;
    dubbingApi
      .updateSettings(selectedProjectId, next)
      .catch((err) => toast.error(detailOf(err) ?? t("dubbing.settingsFailed")));
  };

  const handleAssignmentVoiceChange = (ass: Assignment, voiceId: string) => {
    if (!selectedProjectId) return;
    const next = assignments.map((a) => (a.id === ass.id ? { ...a, voice_id: voiceId } : a));
    setAssignments(next);
    dubbingApi.updateAssignments(selectedProjectId, { assignments: next }).catch((err) => {
      console.error(err);
      toast.error(detailOf(err) ?? t("dubbing.roleAssignFailed"));
    });
  };

  // ---------- 早退分支 ----------
  if (loading) {
    return (
      <Page wide>
        <LoaderBlock>{t("app.loading")}</LoaderBlock>
      </Page>
    );
  }

  const noProjects = projects.length === 0 && !bookIdParam;
  const selectedChapter = selectedChapterIndex !== null ? chapters[selectedChapterIndex] : null;
  const chAssignments = selectedChapter ? chapterAssignments(selectedChapter.id) : [];
  const isMulti = selectedProject?.mode === "multi";

  const newProjectDialog = (
    <Modal
      open={showNewDialog}
      onClose={closeNewDialog}
      title={t("dubbing.createTitle")}
      width="30rem"
      footer={
        <>
          <Button variant="ghost" onClick={closeNewDialog}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            loading={creatingProject}
            onClick={handleCreateProject}
            disabled={!newProjectName.trim() || !newProjectBookId}
          >
            {creatingProject ? t("common.creating") : t("dubbing.createSubmit")}
          </Button>
        </>
      }
    >
      <div className="stack-form">
        <Field label={t("dubbing.createName")}>
          <Input
            value={newProjectName}
            placeholder={t("dubbing.createNamePlaceholder")}
            onChange={(e) => setNewProjectName(e.target.value)}
            autoFocus
          />
        </Field>
        <Field
          label={t("dubbing.createBook")}
          hint={books.length === 0 ? t("dubbing.createBookEmpty") : undefined}
        >
          <Select
            value={newProjectBookId}
            placeholder={t("dubbing.createBookPlaceholder")}
            onChange={(e) => setNewProjectBookId(e.target.value)}
            options={books.map((b) => ({
              value: b.id,
              label: `${b.title}（${b.author || t("edit.anonymous")}）`,
            }))}
          />
        </Field>
        <Field label={t("dubbing.createMode")}>
          <Segmented
            name="dubbing-mode"
            value={newProjectMode}
            onChange={setNewProjectMode}
            options={MODES.map((mode) => ({ value: mode, label: tEnum("mode", mode) }))}
          />
        </Field>
      </div>
    </Modal>
  );

  if (noProjects) {
    return (
      <Page wide>
        <PageHeader title={t("dubbing.title")} subtitle={t("dubbing.subtitle")} />
        <EmptyState
          icon={Clapperboard}
          title={t("dubbing.emptyTitle")}
          desc={t("dubbing.emptyDesc")}
          actions={
            <>
              <Button variant="primary" icon={Plus} onClick={openNewDialog}>
                {t("dubbing.emptyCtaCreate")}
              </Button>
              <Button variant="secondary" onClick={() => navigate("/import")}>
                {t("dubbing.emptyCtaImport")}
              </Button>
              <Button variant="ghost" onClick={() => navigate("/edit")}>
                {t("dubbing.emptyCtaEdit")}
              </Button>
            </>
          }
        />
        {newProjectDialog}
      </Page>
    );
  }

  const projectActions = (
    <>
      {isMulti ? (
        <Button variant="secondary" icon={Wand2} onClick={() => setWizardOpen((v) => !v)}>
          {wizardOpen ? t("dubbing.advancedMode") : t("dubbing.wizardMode")}
        </Button>
      ) : null}
      <Button
        variant="secondary"
        onClick={handlePreview}
        loading={previewPlaying}
        disabled={!selectedProjectId || projectLoading}
      >
        {previewPlaying ? t("dubbing.previewWorking") : t("dubbing.previewBtn")}
      </Button>
      <Button
        variant="primary"
        onClick={handleStartConvert}
        loading={converting}
        disabled={!selectedProjectId || projectLoading}
      >
        {converting ? t("dubbing.convertWorking") : t("dubbing.convertBtn")}
      </Button>
    </>
  );

  return (
    <Page wide>
      <PageHeader
        title={t("dubbing.title")}
        subtitle={t("dubbing.subtitle")}
        actions={projectActions}
      />

      <Toolbar className="dub-toolbar">
        <Select
          aria-label={t("dubbing.project")}
          value={selectedProjectId || ""}
          placeholder={t("dubbing.projectPlaceholder")}
          onChange={(e) => handleSelectProject(e.target.value)}
          options={projects.map((p) => ({
            value: p.id,
            label: `${p.name}（${tEnum("mode", p.mode)}）`,
          }))}
        />
        <Button variant="secondary" size="sm" icon={Plus} onClick={openNewDialog}>
          {t("dubbing.newProject")}
        </Button>
        {currentProjectSummary ? (
          <Badge tone="accent">
            {t("dubbing.modeLabel")}：{tEnum("mode", currentProjectSummary.mode)}
          </Badge>
        ) : null}
        {selectedProject ? (
          <StatusBadge status={selectedProject.status} group="projectStatus" />
        ) : null}
        <ToolbarSpacer />
        {book ? (
          <span className="u-xs u-muted">
            {book.title} · {format.number(book.total_chars || 0)} {t("common.charsUnit")}
          </span>
        ) : null}
      </Toolbar>

      {projectLoading ? (
        <LoaderBlock>{t("dubbing.projectLoading")}</LoaderBlock>
      ) : !selectedProject ? (
        <Alert tone="info">{t("common.selectProjectFirst")}</Alert>
      ) : isMulti && wizardOpen ? (
        <CastWizard
          key={selectedProject.id}
          projectId={selectedProject.id}
          projectName={selectedProject.name}
          bookId={selectedProject.book_id || null}
          books={books}
          voices={voices}
          globalSettings={globalSettings}
          onSelectProject={handleSelectProject}
          onConvert={handleStartConvert}
          converting={converting}
        />
      ) : (
        <div className="dub-layout">
          {/* 左栏：书籍信息 + 章节列表 */}
          <div className="dub-col dub-col--scroll">
            {book ? (
              <Card title={t("dubbing.bookInfo")}>
                <DetailList
                  items={[
                    { label: t("edit.author"), value: book.author || t("edit.anonymous") },
                    { label: t("edit.format"), value: book.format?.toUpperCase() || "—" },
                    { label: t("edit.totalChars"), value: format.number(book.total_chars || 0), mono: true },
                    { label: t("common.language"), value: tEnum("language", book.language || "", t("common.unknown")) },
                  ]}
                />
              </Card>
            ) : null}

            <Card
              title={`${t("dubbing.chaptersTitle")}（${chapters.length}）`}
              body="tight"
            >
              {chapters.length === 0 ? (
                <p className="hint u-center">{t("dubbing.chapterEmpty")}</p>
              ) : (
                <div className="pick-list">
                  {chapters.map((ch, idx) => {
                    const count = chapterAssignments(ch.id).length;
                    return (
                      <button
                        type="button"
                        key={ch.id}
                        className="pick-row"
                        data-active={selectedChapterIndex === idx ? "true" : "false"}
                        onClick={() => setSelectedChapterIndex(idx)}
                      >
                        <span className="pick-row__body">
                          <span className="pick-row__title">
                            #{idx + 1} {ch.title}
                          </span>
                          <span className="u-xs u-muted">
                            {format.number(ch.char_count || 0)} {t("common.charsUnit")}
                            {count > 0 ? ` · ${t("dubbing.assignmentCount", { count })}` : ""}
                          </span>
                        </span>
                        {count > 0 ? <Badge tone="accent">{count}</Badge> : null}
                      </button>
                    );
                  })}
                </div>
              )}
            </Card>
          </div>

          {/* 中栏：段列表与文本预览 */}
          <div className="dub-col dub-col--scroll">
            {!selectedChapter ? (
              <EmptyState
                title={t("dubbing.chapterSelectedTitle")}
                desc={t("dubbing.selectChapterHint")}
              />
            ) : (
              <Card
                title={`#${(selectedChapterIndex ?? 0) + 1} ${selectedChapter.title}`}
                desc={`${format.number(selectedChapter.char_count || 0)} ${t("common.charsUnit")}`}
              >
                {chAssignments.length > 0 ? (
                  <>
                    <div className="u-xs u-muted">{t("dubbing.barLabel")}</div>
                    <div className="assign-bars" aria-hidden="true">
                      {chAssignments.map((ass, i) => {
                        const role = roleById(ass.role_id);
                        const total = selectedChapter.char_count || 1;
                        const widthPct = Math.max(((ass.end_char - ass.start_char) / total) * 100, 2);
                        const voice = voiceById(ass.voice_id);
                        return (
                          <span
                            key={ass.id || i}
                            className="assign-bar"
                            style={{ width: `${widthPct}%`, background: roleColor(ass.role_id) }}
                            title={`${role?.name || t("dubbing.roleUnknown")} → ${
                              voice?.name || t("dubbing.voiceUnassigned")
                            } (${ass.start_char}-${ass.end_char})`}
                          >
                            {role?.name || "?"}
                          </span>
                        );
                      })}
                    </div>
                    <div>
                      <h3 className="section-title">{t("dubbing.assignmentDetail")}</h3>
                      {chAssignments.map((ass, i) => {
                        const role = roleById(ass.role_id);
                        const voice = voiceById(ass.voice_id);
                        return (
                          <div className="assign-row" key={ass.id || i}>
                            <span
                              className="swatch"
                              style={{ background: roleColor(ass.role_id) }}
                              aria-hidden="true"
                            />
                            <span className="u-xs u-muted">
                              {t("dubbing.rangeLabel", { start: ass.start_char, end: ass.end_char })}
                            </span>
                            <span className="u-strong">{role?.name || t("dubbing.roleUnknown")}</span>
                            <span aria-hidden="true">→</span>
                            <span className="u-sm">{voice?.name || t("dubbing.voiceUnassigned")}</span>
                          </div>
                        );
                      })}
                    </div>
                  </>
                ) : (
                  <EmptyState
                    title={t("dubbing.noAssignment")}
                    desc={t("dubbing.noAssignmentHint")}
                  />
                )}

                <div>
                  <h3 className="section-title">{t("dubbing.textPreview")}</h3>
                  <div className="text-preview">
                    {selectedChapter.content_preview || t("dubbing.noPreview")}
                  </div>
                </div>
              </Card>
            )}
          </div>

          {/* 右栏：角色 / 音色分配 / 全局设置 */}
          <div className="dub-col dub-col--scroll dub-col--last-span">
            <Card title={t("dubbing.assignmentsTitle")} body="tight">
              <Tabs
                ariaLabel={t("dubbing.assignmentsTitle")}
                variant="pill"
                active={activeTab}
                onChange={(id) => setActiveTab(id as typeof activeTab)}
                items={[
                  { id: "roles", label: t("dubbing.tabRoles"), count: roles.length },
                  { id: "assignments", label: t("dubbing.tabAssignments"), count: chAssignments.length },
                  { id: "settings", label: t("dubbing.tabSettings") },
                ]}
              />
              <TabPanels>
                <TabPanel id="roles" active={activeTab}>
                  <div className="stack-form">
                    <div className="u-row u-gap-2 u-wrap">
                      <Input
                        className="u-grow"
                        value={newRoleName}
                        placeholder={t("dubbing.roleNamePlaceholder")}
                        aria-label={t("dubbing.addRole")}
                        onChange={(e) => setNewRoleName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleAddRole();
                        }}
                      />
                      <Select
                        value={newRoleType}
                        onChange={(e) => setNewRoleType(e.target.value)}
                        options={ROLE_TYPES.map((rt) => ({ value: rt, label: tEnum("roleType", rt) }))}
                      />
                      <Button
                        variant="primary"
                        onClick={handleAddRole}
                        loading={addingRole}
                        disabled={!newRoleName.trim()}
                      >
                        {t("common.add")}
                      </Button>
                    </div>
                  </div>

                  <h3 className="section-title u-mt-4">
                    {t("dubbing.roleList")}（{roles.length}）
                  </h3>
                  {roles.length === 0 ? (
                    <p className="hint">{t("dubbing.roleEmpty")}</p>
                  ) : (
                    roles.map((role) => (
                      <div className="assign-row" key={role.id}>
                        <span className="u-strong u-truncate">{role.name}</span>
                        <Badge tone="info">{tEnum("roleType", role.role_type, role.role_type)}</Badge>
                        <Select
                          className="u-grow"
                          value={role.voice_id || ""}
                          placeholder={t("dubbing.roleVoicePlaceholder")}
                          aria-label={`${role.name} · ${t("dubbing.roleVoice")}`}
                          onChange={(e) => handleVoiceSelectForRole(role.id, e.target.value)}
                          options={voiceOptionsFor(role.voice_id)}
                        />
                        {role.voice_id ? (
                          <Button size="sm" variant="primary" onClick={() => handleAssignVoice(role)}>
                            {t("common.confirm")}
                          </Button>
                        ) : null}
                        <IconButton
                          icon={Trash2}
                          variant="danger-soft"
                          label={t("dubbing.roleDelete")}
                          onClick={() => handleDeleteRole(role)}
                        />
                      </div>
                    ))
                  )}
                </TabPanel>

                <TabPanel id="assignments" active={activeTab}>
                  {!selectedChapter ? (
                    <p className="hint">{t("dubbing.selectChapterHint")}</p>
                  ) : chAssignments.length === 0 ? (
                    <p className="hint">{t("dubbing.noAssignment")}</p>
                  ) : (
                    chAssignments.map((ass, i) => {
                      const role = roleById(ass.role_id);
                      const current = voiceById(ass.voice_id);
                      return (
                        <div className="assign-row" key={ass.id || i}>
                          <span className="swatch" style={{ background: roleColor(ass.role_id) }} aria-hidden="true" />
                          <span className="u-strong u-truncate">
                            {role?.name || t("dubbing.roleUnknown")}
                          </span>
                          <span className="u-xs u-muted">
                            {t("dubbing.rangeLabel", { start: ass.start_char, end: ass.end_char })}
                          </span>
                          <Select
                            className="u-grow"
                            value={ass.voice_id || ""}
                            placeholder={t("dubbing.assignmentVoicePlaceholder")}
                            aria-label={`${role?.name || ""} · ${t("dubbing.assignmentsTitle")}`}
                            onChange={(e) => handleAssignmentVoiceChange(ass, e.target.value)}
                            options={voiceOptionsFor(ass.voice_id)}
                          />
                          {current ? (
                            <span className="u-xs u-muted">
                              {t("dubbing.currentVoice", { name: current.name })}
                            </span>
                          ) : null}
                        </div>
                      );
                    })
                  )}
                </TabPanel>

                <TabPanel id="settings" active={activeTab}>
                  <div className="stack-form">
                    <Slider
                      label={t("dubbing.settingsSpeed")}
                      min={0.5}
                      max={2}
                      step={0.1}
                      value={globalSettings.speed}
                      onValueChange={(speed) => setGlobalSettings((prev) => ({ ...prev, speed }))}
                      valueText={`${globalSettings.speed.toFixed(1)}×`}
                      minText="0.5×"
                      maxText="2.0×"
                    />
                    <Slider
                      label={t("dubbing.settingsPitch")}
                      min={-12}
                      max={12}
                      step={1}
                      value={globalSettings.pitch}
                      onValueChange={(pitch) => setGlobalSettings((prev) => ({ ...prev, pitch }))}
                      valueText={globalSettings.pitch > 0 ? `+${globalSettings.pitch}` : String(globalSettings.pitch)}
                      minText="-12"
                      maxText="+12"
                    />
                    <Field label={t("dubbing.settingsEmotion")}>
                      <Select
                        value={globalSettings.emotion}
                        onChange={(e) =>
                          setGlobalSettings((prev) => ({ ...prev, emotion: e.target.value }))
                        }
                        options={EMOTION_OPTIONS.map((emo) => ({
                          value: emo,
                          label: tEnum("emotion", emo),
                        }))}
                      />
                    </Field>
                    <Field label={t("ttsMode.label")}>
                      <TtsModeToggle
                        name="dubbing-tts-tier"
                        value={globalSettings.tier}
                        onChange={persistTier}
                      />
                    </Field>
                    <p className="hint">{t("dubbing.settingsHint")}</p>
                    <Button
                      variant="primary"
                      block
                      loading={settingsSaving}
                      onClick={handleSaveSettings}
                    >
                      {settingsSaving ? t("common.saving") : t("dubbing.settingsSave")}
                    </Button>
                  </div>
                </TabPanel>
              </TabPanels>
            </Card>
          </div>
        </div>
      )}

      {newProjectDialog}

      {/* 试听预览用的隐藏播放器 */}
      <audio ref={audioRef} preload="none" />
    </Page>
  );
}
