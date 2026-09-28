/**
 * 配音模式开关（指令#1：默认在线 + 保留本地，两套可选）。
 *
 * 值就是发给后端的 `settings.tier`（在线/本地），后端真值在
 * `modules/tts_engine/tier.py`；两档各自用哪台引擎、引擎当前可用吗，来自
 * `GET /api/system/engines`（AppShell 开机问一次，存在 appStore.ttsModes）。
 *
 * 拿不到元数据时开关照常可切（不挡用户的路上），只是不显示引擎名 ——
 * 宁可留白，也不写一台"猜测的引擎"给用户看。
 */
import { Cloud, HardDrive } from "lucide-react";
import { modeOfTier, useAppStore, type TtsTier } from "../store/appStore";
import { useI18n } from "../i18n";
import { Segmented } from "./ui";

export default function TtsModeToggle({
  name,
  value,
  onChange,
  showHint = true,
}: {
  name: string;
  value: string;
  onChange: (tier: TtsTier) => void;
  showHint?: boolean;
}) {
  const { t } = useI18n();
  const ttsModes = useAppStore((state) => state.ttsModes);
  const mode = modeOfTier(ttsModes, value);

  return (
    <div className="u-col u-gap-2">
      <Segmented
        name={name}
        value={value}
        onChange={(next) => onChange(next === "local" ? "local" : "online")}
        options={[
          {
            value: "online",
            label: t("ttsMode.online"),
            desc: t("ttsMode.onlineDesc"),
            icon: Cloud,
          },
          {
            value: "local",
            label: t("ttsMode.local"),
            desc: t("ttsMode.localDesc"),
            icon: HardDrive,
          },
        ]}
      />
      {showHint ? (
        <div className="u-col u-gap-1">
          <p className="hint">
            {mode
              ? t(mode.available
                  ? "ttsMode.engineHint"
                  : "ttsMode.engineUnavailable", {
                  engine: mode.engine_display || mode.engine,
                  reason: mode.reason ?? "",
                })
              : t("ttsMode.metaMissing")}
          </p>
          {value === "local" ? (
            <p className="hint">{t("ttsMode.localCostHint")}</p>
          ) : null}
          {value === "online" ? (
            <p className="hint">{t("ttsMode.tierDowngradeHint")}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
