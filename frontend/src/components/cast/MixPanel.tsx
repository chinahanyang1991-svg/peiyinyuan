/**
 * MixPanel —— 音乐与混音面板（向导第 4 步）
 *
 * 对应 backend：GET/PUT /api/dubbing/projects/{id}/mix-settings（写 project.global_settings.mix）
 *              GET /api/bgm | POST /api/bgm/upload | GET /api/bgm/{id}/preview
 * 混音语义与参数范围来自 modules/mixer/audio_mix.py 的 schema 注释与 validate_settings：
 *   bgm{ref,gain_db,duck_db,ducking} / sfx{items,chapter_marks,...} /
 *   role_gains{名字:{gain_db}} / master{gain_db,target_lufs} / gaps{dialogue,narration,default}
 *
 * 直通（passthrough）= 不配 BGM/SFX、增益全 0、LUFS 未设 —— 此时后端逐样本走现状
 * concat 路径，面板会明确提示"不改变现有行为"，让用户知道自己在优化而非改坏。
 */

import { useCallback, useMemo, useRef } from "react";
import { Upload } from "lucide-react";
import type { BgmItem, CastRole, MixSettings } from "../../services/api";
import { useI18n } from "../../i18n";
import {
  Alert,
  AudioRow,
  Button,
  Field,
  NumberInput,
  SectionTitle,
  Select,
  Slider,
  Switch,
} from "../ui";
import VirtualList from "./VirtualList";

interface MixPanelProps {
  mix: MixSettings;
  roles: CastRole[];
  bgmItems: BgmItem[];
  sfxItems: BgmItem[];
  ffmpegAvailable?: boolean;
  librarySr?: number;
  passthrough?: boolean;
  onChange: (next: MixSettings) => void;
  onRoleMix: (role: CastRole, mix: { gain_db?: number; speed?: number; pitch?: number }) => void;
  onUploadBgm: (file: File) => void;
  height: number;
}

const GAIN_MIN = -18;
const GAIN_MAX = 6;
const LUFS_MIN = -40;
const LUFS_MAX = -6;

function clampNumber(value: string, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

export default function MixPanel({
  mix,
  roles,
  bgmItems,
  sfxItems,
  ffmpegAvailable,
  librarySr,
  passthrough,
  onChange,
  onRoleMix,
  onUploadBgm,
  height,
}: MixPanelProps) {
  const { t, tEnum, format } = useI18n();
  const fileRef = useRef<HTMLInputElement | null>(null);

  const bgm = mix.bgm || {};
  const sfx = mix.sfx || {};
  const master = mix.master || {};
  const gaps = mix.gaps || {};
  const roleGains = mix.role_gains || {};

  type BgmPatch = Partial<NonNullable<MixSettings["bgm"]>>;
  const patch = useCallback(
    (part: Partial<MixSettings>) => onChange({ ...mix, ...part }),
    [mix, onChange]
  );
  const patchBgm = useCallback(
    (part: BgmPatch) => patch({ bgm: { ...(mix.bgm || {}), ...part } }),
    [mix, patch]
  );
  const patchMaster = useCallback(
    (part: Record<string, any>) => patch({ master: { ...(mix.master || {}), ...part } }),
    [mix, patch]
  );
  const patchGaps = useCallback(
    (part: Record<string, number>) => patch({ gaps: { ...(mix.gaps || {}), ...part } }),
    [mix, patch]
  );
  const patchRoleGain = useCallback(
    (name: string, gain: number) => {
      const next = { ...(mix.role_gains || {}) };
      if (Math.abs(gain) < 1e-6) delete next[name];
      else next[name] = { gain_db: Number(gain.toFixed(2)) };
      patch({ role_gains: next });
    },
    [mix, patch]
  );

  const bgmOptions = useMemo(() => bgmItems, [bgmItems]);
  const gainRows = useMemo(() => roles, [roles]);
  const selectedBgm = bgmOptions.filter((b) => b.id === bgm.ref);
  const srText = librarySr ? (librarySr / 1000).toFixed(0) : "48";

  return (
    <div className="mix-grid">
      {/* --- 背景音乐 --- */}
      <div className="mix-group">
        <SectionTitle>{t("mix.bgmTitle")}</SectionTitle>
        <p className="hint">
          {t("mix.bgmHint", {
            sr: srText,
            noffmpeg: ffmpegAvailable === false ? t("mix.bgmNoFfmpeg") : "",
          })}
        </p>

        <Field label="BGM">
          <Select
            value={bgm.ref || ""}
            placeholder={t("mix.bgmNone")}
            onChange={(e) => patchBgm({ ref: e.target.value || null })}
            options={bgmOptions.map((b) => ({
              value: b.id,
              label: `${b.name}${b.moods?.length ? `（${b.moods.join("/")}）` : ""}${
                b.placeholder ? ` · ${t("mix.bgmPlaceholderTag")}` : ""
              }`,
            }))}
          />
        </Field>

        <Slider
          label={t("mix.bgmGain")}
          min={-36}
          max={0}
          step={0.5}
          value={bgm.gain_db ?? -18}
          disabled={!bgm.ref}
          onValueChange={(v) => patchBgm({ gain_db: clampNumber(String(v), -18) })}
          valueText={t("mix.dB", { value: format.decimal(bgm.gain_db ?? -18, 1) })}
        />

        <div className="mix-fields">
          <Switch
            label={t("mix.bgmDucking")}
            checked={bgm.ducking !== false}
            disabled={!bgm.ref}
            onChange={(e) => patchBgm({ ducking: e.target.checked })}
          />
          <Slider
            label={t("mix.bgmDuckAmount")}
            min={0}
            max={24}
            step={1}
            value={bgm.duck_db ?? 8}
            disabled={!bgm.ref || bgm.ducking === false}
            onValueChange={(v) => patchBgm({ duck_db: clampNumber(String(v), 8) })}
            valueText={t("mix.dB", { value: Math.round(bgm.duck_db ?? 8) })}
          />
          <Switch
            label={t("mix.bgmLoop")}
            checked={bgm.loopable !== false}
            disabled={!bgm.ref}
            onChange={(e) => patchBgm({ loopable: e.target.checked })}
          />
        </div>

        <div>
          <Button
            variant="outline"
            size="sm"
            icon={Upload}
            onClick={() => fileRef.current?.click()}
          >
            {t("mix.bgmUploadHint")}
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept="audio/*"
            className="sr-only"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) onUploadBgm(f);
            }}
          />
        </div>

        {selectedBgm.map((b) => (
          <AudioRow
            key={b.id}
            src={`/api/bgm/${encodeURIComponent(b.id)}/preview`}
            title={b.name}
          />
        ))}
      </div>

      {/* --- 章节音效 --- */}
      <div className="mix-group">
        <SectionTitle>{t("mix.sfxTitle")}</SectionTitle>
        <p className="hint">{t("mix.sfxHint")}</p>
        <Switch
          label={t("mix.sfxMarks")}
          checked={!!sfx.chapter_marks}
          onChange={(e) => patch({ sfx: { ...sfx, chapter_marks: e.target.checked } })}
        />
        <div className="mix-fields">
          <Field label={t("mix.sfxStart")}>
            <Select
              value={sfx.start_ref || ""}
              placeholder={t("mix.sfxStartDefault")}
              disabled={!sfx.chapter_marks}
              onChange={(e) => patch({ sfx: { ...sfx, start_ref: e.target.value || undefined } })}
              options={sfxItems.map((s) => ({ value: s.id, label: s.name }))}
            />
          </Field>
          <Field label={t("mix.sfxEnd")}>
            <Select
              value={sfx.end_ref || ""}
              placeholder={t("mix.sfxEndDefault")}
              disabled={!sfx.chapter_marks}
              onChange={(e) => patch({ sfx: { ...sfx, end_ref: e.target.value || undefined } })}
              options={sfxItems.map((s) => ({ value: s.id, label: s.name }))}
            />
          </Field>
        </div>
      </div>

      {/* --- 角色增益表 --- */}
      <div className="mix-group mix-group--wide">
        <SectionTitle>{t("mix.gainTitle")}</SectionTitle>
        <p className="hint">{t("mix.gainHint")}</p>
        <div className="gain-table">
          <div className="gain-head">
            <span>{t("mix.gainColRole")}</span>
            <span>{t("mix.gainColNow")}</span>
            <span>{t("mix.gainColGain")}</span>
            <span>{t("mix.gainColSpeed")}</span>
            <span>{t("mix.gainColPitch")}</span>
          </div>
          <VirtualList
            items={gainRows}
            rowHeight={48}
            columns={1}
            height={Math.min(height, 260)}
            keyOf={(r: CastRole) => r.id}
            empty={<p className="hint">{t("common.empty")}</p>}
            renderItem={(r: CastRole) => (
              <div className="gain-row">
                <span className="gain-row__name" title={`${r.name}（${tEnum("roleType", r.role_type)}）`}>
                  {r.role_type === "narrator"
                    ? t("mix.gainNarrator", { name: r.name })
                    : r.name}
                </span>
                <span className="gain-row__now">
                  {t("mix.gainRoleValue", {
                    mix: format.decimal(roleGains[r.name]?.gain_db ?? 0, 1),
                    role: format.decimal(r.mix?.gain_db ?? 0, 1),
                  })}
                </span>
                <Slider
                  min={GAIN_MIN}
                  max={GAIN_MAX}
                  step={0.5}
                  value={roleGains[r.name]?.gain_db ?? 0}
                  aria-label={`${r.name} · ${t("mix.gainColGain")}`}
                  valueText={t("mix.dB", { value: format.decimal(roleGains[r.name]?.gain_db ?? 0, 1) })}
                  onValueChange={(v) => patchRoleGain(r.name, clampNumber(String(v), 0))}
                />
                <NumberInput
                  value={r.mix?.speed ?? 1}
                  min={0.5}
                  max={2}
                  step={0.05}
                  suffix="×"
                  ariaLabel={`${r.name} · ${t("mix.speedTitle")}`}
                  title={t("mix.speedTitle")}
                  onValueChange={(v) => onRoleMix(r, { ...r.mix, speed: v })}
                />
                <NumberInput
                  value={r.mix?.pitch ?? 0}
                  min={-12}
                  max={12}
                  step={0.5}
                  ariaLabel={`${r.name} · ${t("mix.pitchTitle")}`}
                  title={t("mix.pitchTitle")}
                  onValueChange={(v) => onRoleMix(r, { ...r.mix, pitch: v })}
                />
              </div>
            )}
          />
        </div>
      </div>

      {/* --- 母带与间隙 --- */}
      <div className="mix-group">
        <SectionTitle>{t("mix.masterTitle")}</SectionTitle>
        <div className="mix-fields">
          <Slider
            label={t("mix.masterGain")}
            min={-12}
            max={6}
            step={0.5}
            value={master.gain_db ?? 0}
            onValueChange={(v) => patchMaster({ gain_db: clampNumber(String(v), 0) })}
            valueText={t("mix.dB", { value: format.decimal(master.gain_db ?? 0, 1) })}
          />
          <Slider
            label={t("mix.lufsTarget")}
            min={LUFS_MIN}
            max={LUFS_MAX}
            step={0.5}
            value={master.target_lufs ?? -16}
            disabled={master.target_lufs == null}
            onValueChange={(v) => patchMaster({ target_lufs: clampNumber(String(v), -16) })}
            valueText={`${master.target_lufs ?? -16} LUFS`}
          />
          <Switch
            label={t("mix.lufsEnable")}
            checked={master.target_lufs != null}
            onChange={(e) =>
              patchMaster({ target_lufs: e.target.checked ? (master.target_lufs ?? -16) : null })
            }
          />
        </div>

        <SectionTitle>{t("mix.gapTitle")}</SectionTitle>
        <div className="mix-fields">
          <Slider
            label={t("mix.gapDialogue")}
            min={0}
            max={1200}
            step={10}
            value={gaps.dialogue ?? 250}
            onValueChange={(v) => patchGaps({ dialogue: clampNumber(String(v), 250) })}
            valueText={t("mix.ms", { value: Math.round(gaps.dialogue ?? 250) })}
          />
          <Slider
            label={t("mix.gapNarration")}
            min={0}
            max={1500}
            step={10}
            value={gaps.narration ?? 400}
            onValueChange={(v) => patchGaps({ narration: clampNumber(String(v), 400) })}
            valueText={t("mix.ms", { value: Math.round(gaps.narration ?? 400) })}
          />
        </div>

        <Alert tone={passthrough ? "info" : "success"}>{passthrough ? t("mix.hintPassthrough") : t("mix.hintEnabled")}</Alert>
      </div>
    </div>
  );
}
