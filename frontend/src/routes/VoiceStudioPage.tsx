/**
 * 音色工作室（UI-100 阶段3 重制）
 * 契约保持：
 *  - voiceApi.list 的 category/language/gender/source/engine 查询参数不变；
 *  - 五类音色 human/animal/electronic/anime/mechanical 必须可筛；
 *  - 试听 /api/voices/{id}/preview、语音设计 /voices/design、克隆 /voices/clone、
 *    在线检索 /voices/online/search、下载 /voices/online/download 全部保留。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AudioLines,
  Download,
  Library,
  Mic,
  Search,
  Sparkles,
  Wand2,
} from "lucide-react";
import { voiceApi } from "../services/api";
import { modeOfTier, useAppStore } from "../store/appStore";
import { ModelStatusBar, SeedControl, VoiceBindingBadge, useVoicePreview } from "../components/ModelStatusBar";
import { useI18n } from "../i18n";
import { useToast } from "../components/ui/Toast";

import {
  Alert,
  AudioRow,
  Badge,
  BadgeStack,
  Button,
  Card,
  ChipButton,
  Dropzone,
  EmptyState,
  Field,
  Input,
  Page,
  PageHeader,
  Select,
  Skeleton,
  StackForm,
  Tabs,
  TabPanel,
  Textarea,
  Toolbar,
  ToolbarSpacer,
} from "../components/ui";

interface Voice {
  id: string;
  name: string;
  source: string;
  category: string;
  engine: string;
  language: string;
  gender: string;
  age_group: string;
  style_tags: string[];
  description: string;
  is_downloaded: boolean;
  has_sample: boolean;
}

interface OnlineResult {
  name: string;
  description?: string;
  downloads?: number;
  stars?: number;
  language?: string;
  source?: string;
}

/** 五类音色机器码 —— 与后端 voice_category 契约一致，不得增删改名 */
const CATEGORY_VALUES = ["human", "animal", "electronic", "anime", "mechanical"] as const;
const LANGUAGE_VALUES = ["zh", "en", "ja", "ko"] as const;
const GENDER_VALUES = ["male", "female"] as const;
const SOURCE_VALUES = ["builtin", "downloaded", "cloned", "designed", "sample"] as const;

/**
 * S2 的闸门：一次提交 1039 张卡 = 33,207 个 DOM 节点 / 1,039 个 `<audio>` /
 * 实测单条 3,642ms long task。渲染量必须与库总量解耦，故按页窗口渲染。
 */
const PAGE_SIZE = 60;

type TabId = "local" | "online" | "design" | "clone";

const SOURCE_TONE: Record<string, "neutral" | "success" | "accent" | "warning" | "info"> = {
  builtin: "neutral",
  downloaded: "success",
  cloned: "accent",
  designed: "warning",
  sample: "info",
};

export default function VoiceStudioPage() {
  const { t, tEnum, tList, format } = useI18n();
  const toast = useToast();

  const [tab, setTab] = useState<TabId>("local");

  /* ---------- 本地库 ---------- */
  const [voices, setVoices] = useState<Voice[]>([]);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState({
    category: "",
    language: "",
    gender: "",
    source: "",
    engine: "",
  });
  /** 库里各引擎各有多少条 —— 引擎筛选的选项源（含零歧义的"选了必有结果"保证） */
  const [engineCounts, setEngineCounts] = useState<Record<string, number>>({});
  const ttsModes = useAppStore((state) => state.ttsModes);
  /** 下载回来的权重/参考音频只有**本地档引擎**会消费（在线档不读本地文件）。
   *  指令#1 之后 `health.tts_engine` 可能是 edgetts，登记目标不能再跟着它走。 */
  const localEngine = modeOfTier(ttsModes, "local")?.engine ?? "";

  useEffect(() => {
    let cancelled = false;
    voiceApi
      .statistics()
      .then((res) => {
        const byEngine = res.data?.statistics?.by_engine;
        if (!cancelled && byEngine && typeof byEngine === "object") {
          setEngineCounts(byEngine as Record<string, number>);
        }
      })
      .catch((err) => {
        // 选项拉不到时筛选框只剩"全部引擎"，不影响列表本身，故只在控制台留痕
        console.warn("拉取引擎统计失败:", err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /* ---------- 在线库 ---------- */
  const [onlineResults, setOnlineResults] = useState<OnlineResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [onlineSearched, setOnlineSearched] = useState(false);

  /* ---------- 设计 ---------- */
  const [designDesc, setDesignDesc] = useState("");
  const [designName, setDesignName] = useState("");
  const [designing, setDesigning] = useState(false);

  /* ---------- 克隆 ---------- */
  const [cloneFile, setCloneFile] = useState<File | null>(null);
  const [cloneName, setCloneName] = useState("");
  const [cloneTranscript, setCloneTranscript] = useState("");
  const [cloning, setCloning] = useState(false);

  const requestSeq = useRef(0);

  /* ---------- L14⑦⑧⑨：模型槽位 / seed 出口 / 排队轻提示 ---------- */
  const preview = useVoicePreview();
  const [seedVoice, setSeedVoice] = useState<Voice | null>(null);
  const [seedInput, setSeedInput] = useState("");
  const [detailSeed, setDetailSeed] = useState<number | null>(null);

  useEffect(() => {
    // 需求 3：voxcpm 互斥闸把试听排到 3~8s，超过阈值才提示，不做假进度条
    if (preview.queued) toast.info(t("voices.previewQueued"));
  }, [preview.queued, toast, t]);

  const handleSeedPreview = async (v: Voice) => {
    setSeedVoice(v);
    setDetailSeed(null);
    const res = await preview.play(v.id);
    // seed 的第二个出口：这条音色还没有成功产物时后端**不发头**（不发比发假值诚实），
    // 此时读 GET /api/voices/{id} 的 seed 键（与 header 同源，都是 VoiceProfile.seed）
    if (res && res.seed === null) {
      try {
        const detail = await voiceApi.get(v.id);
        const s = detail.data?.voice?.seed;
        if (typeof s === "number") setDetailSeed(s);
      } catch (err) {
        /* 详情失败不影响试听本身 */
        console.warn("读取音色 seed 详情失败:", err);
      }
    }
  };

  const loadVoices = useCallback(
    async (searchTerm = "") => {
      const seq = ++requestSeq.current;
      setLoading(true);
      try {
        const params: Record<string, string> = {};
        if (filters.language) params.language = filters.language;
        if (filters.gender) params.gender = filters.gender;
        if (filters.source) params.source = filters.source;
        if (filters.engine) params.engine = filters.engine;
        if (filters.category) params.category = filters.category;
        // L14⑥ (UI100 §6.4): 改走 total 感知的全量分页。旧写法只取首页 limit=100，
        // 517 条音色库在任意筛选下都会被静默截断成"看起来只有 100 个音色"。
        const all = await voiceApi.listAll(params);
        if (seq !== requestSeq.current) return;
        let list: Voice[] = all as Voice[];
        const word = searchTerm.trim();
        if (word) {
          list = list.filter(
            (v) =>
              v.name?.includes(word) ||
              v.description?.includes(word) ||
              v.style_tags?.some((tag) => tag.includes(word))
          );
        }
        setVoices(list);
        setPage(1);
      } catch (err) {
        if (seq !== requestSeq.current) return;
        setVoices([]);
        toast.error(t("common.networkError"));
      }
      if (seq === requestSeq.current) setLoading(false);
    },
    [filters, t, toast]
  );

  useEffect(() => {
    if (tab !== "local") return;
    loadVoices();
    // 仅在进入本地页或筛选变化时重载；搜索词由 handleLocalSearch 显式触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, filters.category, filters.language, filters.gender, filters.source, filters.engine]);

  const handleLocalSearch = () => loadVoices(search);

  const setFilter = (patch: Partial<typeof filters>) =>
    setFilters((prev) => ({ ...prev, ...patch }));

  const categoryOptions = useMemo(
    () => [
      { value: "", label: t("voices.filterCategory") },
      ...CATEGORY_VALUES.map((v) => ({ value: v, label: tEnum("voiceCategory", v) })),
    ],
    [t, tEnum]
  );

  /** 数量多的引擎排前面；同名引擎的次序只由名字决定，避免每次刷新跳序。
   *  "全部引擎"由 Select 的 placeholder 自动给出，这里不再塞一条空值项。 */
  const engineOptions = useMemo(() => {
    const names = Object.keys(engineCounts).filter((n) => n && engineCounts[n] > 0);
    names.sort((a, b) => engineCounts[b] - engineCounts[a] || (a < b ? -1 : 1));
    return names.map((n) => ({ value: n, label: `${n} (${format.number(engineCounts[n])})` }));
  }, [engineCounts, format]);

  /* ---------- 渲染窗口（S2：DOM 量与库总量解耦） ---------- */
  const totalPages = Math.max(1, Math.ceil(voices.length / PAGE_SIZE));
  const pageVoices = useMemo(
    () => voices.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [voices, page]
  );

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const pagerRef = useRef<HTMLDivElement | null>(null);
  const gotoPage = (next: number) => {
    const clamped = Math.min(Math.max(1, next), totalPages);
    setPage(clamped);
    // 翻到下一页后视口还停在长列表底部，会把新窗口整块留在屏幕外
    pagerRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  };

  /* ---------- 在线检索 ---------- */
  const handleOnlineSearch = useCallback(
    async (term?: string) => {
      const query = (term ?? search).trim();
      if (!query) {
        toast.warning(t("voices.searchNeedWord"));
        return;
      }
      setSearching(true);
      try {
        const res = await voiceApi.searchOnline(query);
        const results: OnlineResult[] = res.data.results ?? [];
        setOnlineResults(results);
        setOnlineSearched(true);
        if (results.length === 0) toast.info(t("voices.searchNoResult"));
      } catch (err) {
        setOnlineResults([]);
        setOnlineSearched(true);
        toast.error(t("voices.searchFailed"));
      }
      setSearching(false);
    },
    [search, t, toast]
  );

  const handleDownload = async (modelName: string) => {
    if (!localEngine) {
      // 拿不到本地档引擎名就不要提交：后端的 /online/download 接受**任何**已注册引擎，
      // 传给在线引擎会"成功"入库一条永远用不上的记录（U-6/M-4 口径：不许假成功）。
      toast.error(t("voices.downloadNoLocalEngine"));
      return;
    }
    try {
      // U-11：登记目标 = 真正会读这批文件的引擎。下载回来的是权重/参考音频，
      // 只有本地档引擎消费，所以取本地档而不是 health 里的当前主引擎。
      await voiceApi.download(modelName, localEngine);
      toast.success(t("voices.downloadQueued"));
    } catch (err) {
      console.error("下载音色失败:", err);
      toast.error(t("voices.downloadFailed"));
    }
  };

  /* ---------- 语音设计 ---------- */
  const handleDesign = async () => {
    if (!designDesc.trim()) {
      toast.warning(t("voices.designNeedDesc"));
      return;
    }
    setDesigning(true);
    try {
      await voiceApi.design(designDesc, designName || "");
      setDesignDesc("");
      setDesignName("");
      toast.success(t("voices.designOk"));
      loadVoices();
    } catch {
      toast.error(t("voices.designFailed"));
    }
    setDesigning(false);
  };

  /* ---------- 音色克隆 ---------- */
  const handleClone = async () => {
    if (!cloneFile) {
      toast.warning(t("voices.cloneNeedFile"));
      return;
    }
    setCloning(true);
    try {
      await voiceApi.clone(
        cloneFile,
        cloneName || cloneFile.name.replace(/\.[^.]+$/, ""),
        cloneTranscript || undefined
      );
      setCloneFile(null);
      setCloneName("");
      setCloneTranscript("");
      toast.success(t("voices.cloneOk"));
      loadVoices();
    } catch {
      toast.error(t("voices.cloneFailed"));
    }
    setCloning(false);
  };

  const keywords = tList("voices.onlineKeywordList");

  const tabItems = [
    { id: "local", label: t("voices.tabLocal"), icon: Library, count: voices.length || undefined },
    { id: "online", label: t("voices.tabOnline"), icon: Search },
    { id: "design", label: t("voices.tabDesign"), icon: Wand2 },
    { id: "clone", label: t("voices.tabClone"), icon: Mic },
  ];

  return (
    <Page>
      <PageHeader title={t("voices.title")} subtitle={t("voices.subtitle")} />

      <Tabs
        items={tabItems}
        active={tab}
        onChange={(id) => setTab(id as TabId)}
        ariaLabel={t("voices.title")}
      />

      {/* ---------------- 本地音色库 ---------------- */}
      <TabPanel id="local" active={tab}>
        <Toolbar>
          <form
            className="u-row u-gap-2"
            style={{ flex: "1 1 14rem", minWidth: 0 }}
            onSubmit={(e) => {
              e.preventDefault();
              handleLocalSearch();
            }}
          >
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("voices.searchPlaceholder")}
              aria-label={t("voices.searchPlaceholder")}
              icon={Search}
            />
            <Button type="submit" variant="secondary">
              {t("common.search")}
            </Button>
          </form>
          <ToolbarSpacer />
          <Select
            aria-label={t("voices.filterLanguage")}
            controlSize="sm"
            value={filters.language}
            onChange={(e) => setFilter({ language: e.target.value })}
            placeholder={t("voices.filterLanguage")}
            options={LANGUAGE_VALUES.map((v) => ({ value: v, label: tEnum("language", v) }))}
          />
          <Select
            aria-label={t("voices.filterGender")}
            controlSize="sm"
            value={filters.gender}
            onChange={(e) => setFilter({ gender: e.target.value })}
            placeholder={t("voices.filterGender")}
            options={GENDER_VALUES.map((v) => ({ value: v, label: tEnum("gender", v) }))}
          />
          <Select
            aria-label={t("voices.filterSource")}
            controlSize="sm"
            value={filters.source}
            onChange={(e) => setFilter({ source: e.target.value })}
            placeholder={t("voices.filterSource")}
            options={SOURCE_VALUES.map((v) => ({ value: v, label: tEnum("voiceSource", v) }))}
          />
          {/* U-11：引擎筛选以前只存在于 state 里、没有任何写入口，选项取自 by_engine 而非硬编码 */}
          <Select
            aria-label={t("voices.filterEngine")}
            controlSize="sm"
            value={filters.engine}
            onChange={(e) => setFilter({ engine: e.target.value })}
            placeholder={t("voices.filterEngine")}
            options={engineOptions}
          />
        </Toolbar>

        {/* 五类音色筛选：chip 行在手机上保持可横滑，全部类别 = 空值 */}
        <Field label={t("voices.categoryTitle")}>
          <div className="u-row u-gap-2 u-wrap">
            {categoryOptions.map((opt) => (
              <ChipButton
                key={opt.value || "all"}
                active={filters.category === opt.value}
                onClick={() => setFilter({ category: opt.value })}
              >
                {opt.value === "" ? t("voices.categoryAll") : opt.label}
              </ChipButton>
            ))}
          </div>
        </Field>

        {/* L14⑦⑩: 按"任务实际用的引擎"看 slots + 分段加载明细 */}
        <ModelStatusBar engine={filters.engine || null} className="u-mb-2" />

        {/* L14⑧⑨: 复听出口（响应头 seed / 详情 seed）+ seed 输入的门控 */}
        {seedVoice ? (
          <Card plain>
            <AudioRow
              src={preview.last?.url || voiceApi.previewUrl(seedVoice.id)}
              title={seedVoice.name}
            />
            {/* 无 last 时播的是缓存里的老产物，头描述的不是这次听到的音频 => 不出徽标 */}
            <VoiceBindingBadge binding={preview.last?.voiceBinding} />
            <SeedControl
              engine={seedVoice.engine}
              value={seedInput}
              onChange={setSeedInput}
              lastSeed={preview.last?.seed ?? detailSeed}
            />
            {preview.error ? <p className="hint u-xs">{preview.error}</p> : null}
          </Card>
        ) : null}

        <p className="hint u-mt-2 u-mb-2" aria-live="polite">
          {loading ? (
            t("common.loading")
          ) : (
            <>
              {t("voices.count", { count: voices.length })}
              {totalPages > 1 ? ` · ${t("voices.pageInfo", { page, total: totalPages })}` : ""}
            </>
          )}
        </p>

        {loading ? (
          <div className="voice-grid">
            {Array.from({ length: 8 }, (_, i) => (
              <Card key={i} plain>
                <Skeleton height={16} width="50%" />
                <div style={{ height: 8 }} />
                <Skeleton height={12} />
                <div style={{ height: 8 }} />
                <Skeleton height={12} width="75%" />
              </Card>
            ))}
          </div>
        ) : voices.length === 0 ? (
          <Card plain style={{ borderStyle: "dashed" }}>
            {search || filters.category || filters.language || filters.gender || filters.source || filters.engine ? (
              <EmptyState
                icon={Search}
                title={t("voices.emptyTitle")}
                desc={t("voices.emptyDesc")}
              />
            ) : (
              <EmptyState
                icon={AudioLines}
                title={t("voices.localEmptyTitle")}
                desc={t("voices.localEmptyDesc")}
                actions={
                  <Button variant="primary" onClick={() => setTab("online")}>
                    {t("voices.tabOnline")}
                  </Button>
                }
              />
            )}
          </Card>
        ) : (
          <>
          <div className="voice-grid">
            {pageVoices.map((v) => (
              <Card key={v.id} className="voice-card" data-source={v.source} body="tight">
                <div className="voice-card__head">
                  <BadgeStack>
                    <Badge tone={SOURCE_TONE[v.source] ?? "neutral"}>
                      {tEnum("voiceSource", v.source, v.source)}
                    </Badge>
                    {v.category && v.category !== "human" ? (
                      <Badge tone="info">{tEnum("voiceCategory", v.category, v.category)}</Badge>
                    ) : null}
                  </BadgeStack>
                  <span className="segment-label">{v.engine}</span>
                </div>
                <p className="voice-card__name">{v.name}</p>
                <BadgeStack>
                  <Badge tone="neutral" size="sm">
                    {tEnum("language", v.language, v.language)}
                  </Badge>
                  {v.gender ? (
                    <Badge tone="neutral" size="sm">
                      {tEnum("gender", v.gender, v.gender)}
                    </Badge>
                  ) : null}
                  {v.style_tags?.slice(0, 3).map((tag, i) => (
                    <Badge key={i} tone="accent" size="sm">
                      {tag}
                    </Badge>
                  ))}
                </BadgeStack>
                {v.description ? <p className="voice-card__desc">{v.description}</p> : null}
                <div className="voice-card__foot">
                  {/* S2：`<audio>` 不常驻——整页 60 张卡也只在"正在播的那一张"上挂一枚 */}
                  {preview.playingId === v.id ? (
                    <AudioRow
                      src={preview.last?.url || voiceApi.previewUrl(v.id)}
                      title={v.name}
                    />
                  ) : null}
                  <Button
                    size="xs"
                    variant="ghost"
                    icon={AudioLines}
                    loading={preview.running && seedVoice?.id === v.id}
                    title={t("voices.previewQueued")}
                    onClick={() =>
                      preview.playingId === v.id ? preview.stop() : void handleSeedPreview(v)
                    }
                  >
                    {preview.playingId === v.id ? t("voices.previewStop") : t("roles.previewBtn")}
                  </Button>
                </div>
              </Card>
            ))}
          </div>
          {totalPages > 1 ? (
            <div className="u-row u-gap-2 u-wrap u-mt-3" ref={pagerRef}>
              <Button
                size="sm"
                variant="secondary"
                disabled={page <= 1}
                onClick={() => gotoPage(page - 1)}
              >
                {t("voices.pagePrev")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={page >= totalPages}
                onClick={() => gotoPage(page + 1)}
              >
                {t("voices.pageNext")}
              </Button>
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => gotoPage(1)}>
                {t("voices.pageFirst")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={page >= totalPages}
                onClick={() => gotoPage(totalPages)}
              >
                {t("voices.pageLast")}
              </Button>
            </div>
          ) : null}
          </>
        )}
      </TabPanel>

      {/* ---------------- 在线音色库 ---------------- */}
      <TabPanel id="online" active={tab}>
        <Card>
          <form
            className="u-row u-gap-2 u-wrap"
            onSubmit={(e) => {
              e.preventDefault();
              handleOnlineSearch();
            }}
          >
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("voices.onlinePlaceholder")}
              aria-label={t("voices.onlinePlaceholder")}
              icon={Search}
              style={{ flex: "1 1 16rem", minWidth: 0 }}
            />
            <Button type="submit" variant="primary" loading={searching} icon={Search}>
              {searching ? t("common.searching") : t("common.search")}
            </Button>
          </form>
          {keywords.length > 0 ? (
            <div className="u-row u-gap-2 u-wrap u-mt-2">
              <span className="hint">{t("voices.onlineKeywords")}</span>
              {keywords.map((k) => (
                <ChipButton key={k} onClick={() => handleOnlineSearch(k)}>
                  {k}
                </ChipButton>
              ))}
            </div>
          ) : null}
          <p className="hint u-mt-2">
            {localEngine
              ? t("voices.downloadTarget", { engine: localEngine })
              : t("voices.downloadNoLocalEngine")}
          </p>
        </Card>

        <div className="u-mt-4" style={{ display: "grid", gap: "var(--space-3)" }}>
          {onlineResults.map((r, i) => (
            <div key={`${r.name}-${i}`} className="online-row">
              <div style={{ flex: "1 1 14rem", minWidth: 0 }}>
                <p style={{ fontWeight: "var(--weight-semibold)" }}>{r.name}</p>
                {r.description ? (
                  <p className="hint" style={{ marginTop: 2 }}>
                    {r.description}
                  </p>
                ) : null}
                <div className="u-row u-gap-2 u-wrap u-mt-2">
                  {typeof r.downloads === "number" ? (
                    <Badge tone="neutral">{t("voices.onlineDownloads", { count: format.number(r.downloads) })}</Badge>
                  ) : null}
                  {typeof r.stars === "number" ? (
                    <Badge tone="neutral">{t("voices.onlineStars", { count: format.number(r.stars) })}</Badge>
                  ) : null}
                  {r.language ? <Badge tone="info">{r.language}</Badge> : null}
                  {r.source ? <Badge tone="neutral">{r.source}</Badge> : null}
                </div>
              </div>
              <Button
                variant="primary"
                icon={Download}
                onClick={() => handleDownload(r.name)}
                aria-label={`${t("voices.downloadBtn")} ${r.name}`}
              >
                {t("voices.downloadBtn")}
              </Button>
            </div>
          ))}
          {onlineResults.length === 0 && searching ? <Skeleton height={72} /> : null}
          {onlineResults.length === 0 && !searching ? (
            <Card plain style={{ borderStyle: "dashed" }}>
              <EmptyState
                icon={Search}
                title={onlineSearched ? t("voices.emptyTitle") : t("voices.onlineEmptyTitle")}
                desc={onlineSearched ? t("voices.emptyDesc") : t("voices.onlineEmptyDesc")}
              />
            </Card>
          ) : null}
        </div>
      </TabPanel>

      {/* ---------------- 语音设计 ---------------- */}
      <TabPanel id="design" active={tab}>
        <Card
          title={t("voices.designTitle")}
          desc={t("voices.designDesc")}
          icon={Sparkles}
          style={{ maxWidth: "40rem" }}
        >
          <StackForm>
            <Alert tone="info">{t("voices.designExample")}</Alert>
            <Field label={t("voices.designDescLabel")} htmlFor="design-desc">
              <Textarea
                id="design-desc"
                value={designDesc}
                onChange={(e) => setDesignDesc(e.target.value)}
                placeholder={t("voices.designDescPlaceholder")}
                rows={3}
              />
            </Field>
            <Field label={t("voices.designNameLabel")} hint={t("voices.designNamePlaceholder")}>
              <Input
                value={designName}
                onChange={(e) => setDesignName(e.target.value)}
                placeholder={t("voices.designNamePlaceholder")}
              />
            </Field>
            <div>
              <Button
                variant="primary"
                icon={Wand2}
                loading={designing}
                disabled={!designDesc.trim()}
                onClick={handleDesign}
              >
                {designing ? t("voices.designWorking") : t("voices.designSubmit")}
              </Button>
            </div>
          </StackForm>
        </Card>
      </TabPanel>

      {/* ---------------- 音色克隆 ---------------- */}
      <TabPanel id="clone" active={tab}>
        <Card
          title={t("voices.cloneTitle")}
          desc={t("voices.cloneDesc")}
          icon={Mic}
          style={{ maxWidth: "40rem" }}
        >
          <StackForm>
            <Dropzone
              accept="audio/*"
              state={cloneFile ? "filled" : "idle"}
              title={cloneFile ? cloneFile.name : t("voices.cloneDrop")}
              hint={cloneFile ? format.bytes(cloneFile.size) : t("voices.cloneFormats")}
              onFiles={(files) => {
                const file = files[0];
                if (!file) return;
                setCloneFile(file);
                if (!cloneName) setCloneName(file.name.replace(/\.[^.]+$/, ""));
              }}
            />
            {cloneFile ? (
              <div className="u-row u-center u-gap-2">
                <span className="hint">{cloneFile.name}</span>
                <Button size="xs" variant="ghost" onClick={() => setCloneFile(null)}>
                  {t("common.remove")}
                </Button>
              </div>
            ) : null}
            <Field label={t("voices.cloneNameLabel")}>
              <Input
                value={cloneName}
                onChange={(e) => setCloneName(e.target.value)}
                placeholder={t("voices.cloneNamePlaceholder")}
              />
            </Field>
            <Field
              label={t("voices.cloneTranscriptLabel")}
              hint={t("voices.cloneTranscriptHint")}
              optional
            >
              <Textarea
                value={cloneTranscript}
                onChange={(e) => setCloneTranscript(e.target.value)}
                placeholder={t("voices.cloneTranscriptPlaceholder")}
                rows={2}
              />
            </Field>
            <div>
              <Button
                variant="primary"
                size="lg"
                icon={Mic}
                loading={cloning}
                disabled={!cloneFile}
                onClick={handleClone}
              >
                {cloning ? t("voices.cloneWorking") : t("voices.cloneSubmit")}
              </Button>
            </div>
          </StackForm>
        </Card>
      </TabPanel>
    </Page>
  );
}
