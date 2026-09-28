import { Badge, type Tone } from "./Badge";
import { useI18n } from "../../i18n";

const TONE_BY_STATUS: Record<string, Tone> = {
  completed: "success",
  done: "success",
  running: "accent",
  processing: "accent",
  synthesizing: "accent",
  paused: "warning",
  queued: "neutral",
  pending: "neutral",
  draft: "neutral",
  ready: "info",
  failed: "danger",
  cancelled: "danger",
};

export interface StatusBadgeProps {
  /** 后端机器码，如 running / completed / cast_extract */
  status: string | null | undefined;
  /** 词典里的枚举组，默认 taskStatus */
  group?: "taskStatus" | "projectStatus" | "stage" | "mode" | "profileSource";
  size?: "sm" | "lg";
  dot?: boolean;
  /** 查不到译名时展示的兜底文案 */
  fallback?: string;
}

/** 状态徽章：机器码 -> 本地化文案 + 语义色，未知的码原样显示，绝不空白 */
export function StatusBadge({
  status,
  group = "taskStatus",
  size = "sm",
  dot = true,
  fallback,
}: StatusBadgeProps) {
  const { tEnum } = useI18n();
  if (!status) {
    return (
      <Badge tone="neutral" size={size}>
        {fallback ?? "—"}
      </Badge>
    );
  }
  const label = tEnum(group, status, fallback ?? status);
  const tone = TONE_BY_STATUS[status] ?? "neutral";
  return (
    <Badge tone={tone} size={size} dot={dot}>
      {label}
    </Badge>
  );
}
