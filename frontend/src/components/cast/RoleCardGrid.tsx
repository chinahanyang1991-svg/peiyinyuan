/**
 * RoleCardGrid —— 角色卡网格（向导第 3 步）
 *
 * 每张卡 = 一个 Role：属性徽章（性别/年龄段/身份/性格/戏份/画像来源 + 旁白标记）
 *            + 音色下拉（推荐 Top-N 置顶，带匹配分与打分拆解；可按五类音色筛选）
 *            + 试听 + 上传自定义音色 + 别名编辑。
 *
 * 数据来自 backend/api/cast.py：
 *   GET  /api/dubbing/projects/{id}/cast            -> roles(CastRole[])
 *   GET  /api/dubbing/projects/{id}/cast/suggestions -> {roles:[{role_name, suggestions}]}
 * 修改只回传 patch，落库由父级 CastWizard 统一走 PUT /cast/confirm。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MicVocal, Play } from "lucide-react";
import { detailText, voiceApi } from "../../services/api";
import type { CastRole, VoiceSuggestion } from "../../services/api";
import { useI18n } from "../../i18n";
import {
  Badge,
  Button,
  Chip,
  ChipButton,
  EmptyState,
  Field,
  FilterRow,
  Input,
  Select,
} from "../ui";
import VirtualList from "./VirtualList";

export interface CastVoice {
  id: string;
  name: string;
  engine?: string;
  gender?: string;
  age_group?: string;
  language?: string;
  source?: string;
  /** 音色五类机器码：human / animal / electronic / anime / mechanical（老数据可能缺省） */
  category?: string;
  style_tags?: string[];
}

/** 五类音色机器码 —— 与后端 voice_category 契约一致，不得增删改名 */
const CATEGORY_VALUES = ["human", "animal", "electronic", "anime", "mechanical"] as const;
const ROLE_TYPES = ["narrator", "hero", "supporting", "custom"] as const;
const UNKNOWN_ATTR_KEYS = ["gender", "age_group", "identity"] as const;

/** 行高含间距，与 pages.css 的 .role-card 固定高度成对，窗口化滚动才可预测 */
const CARD_HEIGHT = 348;
/** 窄屏单列时徽章与别名要换行，卡体会超过 21rem；CSS 加高 .role-card 的同时行高也要跟着放大，否则行与行重叠 */
const CARD_HEIGHT_NARROW = 428;
const NARROW_QUERY = "(max-width: 767.98px)";
const MIN_CARD_WIDTH = 300;

interface RoleCardGridProps {
  roles: CastRole[];
  suggestions: Record<string, VoiceSuggestion[]>;
  voices: CastVoice[];
  onChange: (role: CastRole, patch: Record<string, any>) => void;
  onPreview: (role: CastRole) => void;
  previewingId?: string | null;
  uploadingId?: string | null;
  onUploaded?: (message: string) => void;
  height: number;
}

export default function RoleCardGrid({
  roles,
  suggestions,
  voices,
  onChange,
  onPreview,
  previewingId,
  uploadingId,
  onUploaded,
  height,
}: RoleCardGridProps) {
  const { t, tEnum } = useI18n();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadTarget = useRef<CastRole | null>(null);
  /** 音色五类筛选：空串 = 全部；缺 category 的老数据按 human 归档 */
  const [category, setCategory] = useState("");
  const [uploading, setUploading] = useState<string | null>(null);
  /** 行高跟随手机断点：CSS 在 <=767.98px 把 .role-card 加高，窗口化的行也必须同步放大 */
  const [rowHeight, setRowHeight] = useState(CARD_HEIGHT);
  useEffect(() => {
    const mq = window.matchMedia(NARROW_QUERY);
    const sync = () => setRowHeight(mq.matches ? CARD_HEIGHT_NARROW : CARD_HEIGHT);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const voiceNameById = useMemo(() => {
    const map: Record<string, string> = {};
    voices.forEach((v) => {
      map[v.id] = v.name;
    });
    return map;
  }, [voices]);

  /** L14⑪: 选项上限 —— 517 项原生 select 在渲染时冻结页面，先做搜索过滤版 */
  const VOICE_OPTION_CAP = 60;
  const [voiceQuery, setVoiceQuery] = useState("");

  /**
   * L14⑪: 每张角色卡的音色下拉此前直出**全库 517 项**（一次 render 517 个 <option>），
   * 现在改为"分类 + 关键词"过滤后再截断到 VOICE_OPTION_CAP 项。
   * 后续项（已登记 L14-REPORT §4）：把原生 select 换成可搜索 combobox / 用
   * components/cast/VirtualList 虚拟化选项列表（需先给音色列表接上服务端分页）。
   */
  const visibleVoices = useMemo(() => {
    const word = voiceQuery.trim().toLowerCase();
    return voices
      .filter((v) => (category ? (v.category || "human") === category : true))
      .filter((v) => {
        if (!word) return true;
        const haystack = [
          v.name,
          v.engine || "",
          (v as { description?: string }).description || "",
          ...(((v as { style_tags?: string[] }).style_tags) || []),
        ]
          .join(" ")
          .toLowerCase();
        return haystack.includes(word);
      })
      .slice(0, VOICE_OPTION_CAP);
  }, [voices, category, voiceQuery]);

  const startUpload = useCallback((role: CastRole) => {
    uploadTarget.current = role;
    fileRef.current?.click();
  }, []);

  const handleFile = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      const role = uploadTarget.current;
      e.target.value = "";
      if (!file || !role) return;
      const name = `${role.name}-custom-${Date.now().toString(36).slice(-4)}`;
      setUploading(role.id);
      try {
        const res = await voiceApi.analyzeAndSave(file, name, "zh");
        const data = res.data?.voice || res.data;
        const vid = data?.voice_id || data?.id;
        if (!vid) throw new Error(t("roles.uploadNoId"));
        onChange(role, { voice_id: vid });
        onUploaded?.(t("roles.uploadedOk", { name: data?.name || name }));
      } catch (err: any) {
        onUploaded?.(
          t("roles.uploadFailed", {
            reason:
              detailText(err?.response?.data?.detail) || err?.message || String(err),
          })
        );
      }
      setUploading(null);
    },
    [onChange, onUploaded, t]
  );

  const renderCard = useCallback(
    (role: CastRole) => {
      const profile = role.cast || {};
      const ranked = suggestions[role.name] || [];
      const picked = role.voice_id || "";
      const isNarrator = role.role_type === "narrator";
      const unknownCount = UNKNOWN_ATTR_KEYS.filter((k) => (profile as any)[k] === "unknown").length;
      /** 后端画像未识别时 identity 是机器码 "unknown"，不得直出，需走 common.unknown */
      const identityKnown = Boolean(profile.identity) && profile.identity !== "unknown";
      const aliasDraft = role.aliases || [];
      const rankedIds = ranked.map((s) => s.voice_id);
      const uploadingSelf = Boolean(uploadingId === role.id || uploading === role.id);

      return (
        <div className={picked ? "role-card" : "role-card role-card--pending"}>
          <div className="role-card__head">
            <span className="role-card__avatar" aria-hidden="true">
              {role.name.slice(0, 1)}
            </span>
            <input
              className="role-card__name"
              value={role.name}
              aria-label={t("roles.nameAria")}
              onChange={(e) => onChange(role, { name: e.target.value })}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v && v !== role.name) onChange(role, { name: v });
              }}
            />
            <Select
              controlSize="sm"
              aria-label={t("roles.typeAria")}
              value={ROLE_TYPES.includes(role.role_type as any) ? role.role_type : "custom"}
              onChange={(e) => onChange(role, { role_type: e.target.value })}
              options={ROLE_TYPES.map((rt) => ({ value: rt, label: tEnum("roleType", rt) }))}
            />
          </div>

          <div className="role-card__badges">
            <Badge tone={profile.gender && profile.gender !== "unknown" ? "info" : "warning"}>
              {tEnum("gender", profile.gender || "unknown")}
            </Badge>
            <Badge tone={profile.age_group && profile.age_group !== "unknown" ? "info" : "warning"}>
              {tEnum("ageGroup", profile.age_group || "unknown")}
            </Badge>
            <Badge tone={identityKnown ? "neutral" : "warning"} title={profile.identity}>
              {(identityKnown ? profile.identity : t("common.unknown"))!.slice(0, 12)}
            </Badge>
            {(profile.personality_tags || []).slice(0, 3).map((tag) => (
              <Badge key={tag} tone="neutral">
                {tag}
              </Badge>
            ))}
            <Badge tone="success" title={t("roles.linesTitle")}>
              {t("roles.linesBadge", {
                lines: profile.lines_count ?? 0,
                chapters: profile.chapters_count ?? 0,
              })}
            </Badge>
            <Badge tone="neutral">
              {tEnum("profileSource", profile.profile_source || "", t("common.unknown"))}
            </Badge>
            {unknownCount > 0 ? (
              <Badge tone="warning" title={t("roles.pendingTitle")}>
                {t("roles.pendingBadge", { count: unknownCount })}
              </Badge>
            ) : null}
            {isNarrator ? (
              <Badge tone="accent" title={t("roles.narratorTitle")}>
                {t("roles.narratorBadge")}
              </Badge>
            ) : null}
          </div>

          <Field
            label={ranked.length ? t("roles.voiceLabelRanked") : t("roles.voiceLabel")}
            hint={picked ? t("roles.voiceCurrent", { name: voiceNameById[picked] || `${picked.slice(0, 8)}…` }) : t("roles.voicePending")}
          >
            <Select
              controlSize="sm"
              value={picked}
              onChange={(e) => onChange(role, { voice_id: e.target.value })}
            >
              <option value="">{t("roles.voiceNone")}</option>
              {picked && !voiceNameById[picked] && !rankedIds.includes(picked) ? (
                /* 音色已被删除但角色仍挂着 id：直出会让下拉错显"未分配"，补一项保住真实状态 */
                <option value={picked}>{t("roles.voiceStale", { id: picked.slice(0, 8) })}</option>
              ) : null}
              {ranked.length > 0 ? (
                <optgroup label={t("roles.voiceGroupTop", { count: ranked.length })}>
                  {ranked.map((s) => (
                    <option key={s.voice_id} value={s.voice_id}>
                      {t("roles.scoreOption", {
                        name: s.voice_name,
                        score: s.score,
                        gender: s.breakdown?.gender ?? 0,
                        age: s.breakdown?.age ?? 0,
                        style: s.breakdown?.style ?? 0,
                        language: s.breakdown?.language ?? 0,
                      })}
                    </option>
                  ))}
                </optgroup>
              ) : null}
              {visibleVoices.length > 0 ? (
                <optgroup label={t("roles.voiceGroupAll")}>
                  {visibleVoices
                    .filter((v) => !rankedIds.includes(v.id))
                    .map((v) => (
                      <option key={v.id} value={v.id}>
                        {`${v.name}（${tEnum("gender", v.gender || "unknown", v.gender || t("common.unknown"))} · ${
                          v.engine || t("common.unknown")
                        }）`}
                      </option>
                    ))}
                </optgroup>
              ) : null}
            </Select>
          </Field>

          <div className="role-card__foot">
            <span className="u-truncate">{picked ? voiceNameById[picked] || `${picked.slice(0, 8)}…` : t("common.none")}</span>
            <div className="u-row u-gap-2">
              <Button
                size="xs"
                variant="secondary"
                icon={Play}
                disabled={!picked}
                loading={previewingId === role.id}
                title={picked ? t("roles.previewHint") : t("roles.previewNeedVoice")}
                onClick={() => onPreview(role)}
              >
                {previewingId === role.id ? t("roles.previewWorking") : t("roles.previewBtn")}
              </Button>
              <Button
                size="xs"
                variant="ghost"
                icon={MicVocal}
                loading={uploadingSelf}
                onClick={() => startUpload(role)}
              >
                {uploadingSelf ? t("roles.uploadWorking") : t("roles.uploadBtn")}
              </Button>
            </div>
          </div>

          <Field label={t("roles.aliasLabel")}>
            <div className="chip-stack">
              {aliasDraft.map((alias) => (
                <Chip
                  key={alias}
                  onRemove={() => onChange(role, { aliases: aliasDraft.filter((x) => x !== alias) })}
                  removeLabel={t("roles.aliasRemove", { name: alias })}
                >
                  {alias}
                </Chip>
              ))}
              <Input
                className="chip__input"
                controlSize="sm"
                placeholder={t("roles.aliasPlaceholder")}
                aria-label={t("roles.aliasPlaceholder")}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    const el = e.currentTarget;
                    const v = el.value.trim();
                    if (v && !aliasDraft.includes(v)) onChange(role, { aliases: [...aliasDraft, v] });
                    el.value = "";
                  }
                }}
              />
            </div>
          </Field>
        </div>
      );
    },
    [onChange, onPreview, previewingId, suggestions, t, tEnum, uploading, uploadingId, visibleVoices, voiceNameById]
  );

  return (
    <div className="wizard__section">
      <input ref={fileRef} type="file" accept="audio/*" className="sr-only" onChange={handleFile} />

      <FilterRow>
        <span className="u-xs u-muted">{t("roles.voiceCategoryTitle")}</span>
        {/* L14⑪: 先搜索过滤，把 517 项下拉压到 <=60 项；完整虚拟化见 L14-REPORT §4 后续项 */}
        <Input
          className="u-xs"
          controlSize="sm"
          style={{ flex: "0 1 12rem", minWidth: "8rem" }}
          value={voiceQuery}
          onChange={(e) => setVoiceQuery(e.target.value)}
          placeholder={t("voices.searchPlaceholder")}
          aria-label={t("voices.searchPlaceholder")}
        />
        <ChipButton active={category === ""} onClick={() => setCategory("")}>
          {t("voices.categoryAll")}
        </ChipButton>
        {CATEGORY_VALUES.map((value) => (
          <ChipButton key={value} active={category === value} onClick={() => setCategory(value)}>
            {tEnum("voiceCategory", value)}
          </ChipButton>
        ))}
        {category && visibleVoices.length === 0 ? (
          <span className="u-xs u-muted">{t("roles.noCategoryVoices")}</span>
        ) : null}
      </FilterRow>

      <VirtualList
        items={roles}
        rowHeight={rowHeight}
        height={height}
        minColumnWidth={MIN_CARD_WIDTH}
        keyOf={(r: CastRole) => r.id}
        renderItem={renderCard}
        empty={<EmptyState title={t("roles.emptyTitle")} desc={t("roles.emptyDesc")} />}
      />
    </div>
  );
}
