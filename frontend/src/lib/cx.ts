/** 极小的 className 合并工具，避免为此引入 clsx 依赖 */
export type ClassValue = string | number | null | undefined | false | ClassArray | ClassRecord;
type ClassArray = ReadonlyArray<ClassValue>;
interface ClassRecord {
  [key: string]: unknown;
}

export function cx(...values: ClassValue[]): string {
  const out: string[] = [];
  const push = (value: ClassValue): void => {
    if (!value && value !== 0) return;
    if (Array.isArray(value)) {
      value.forEach(push);
      return;
    }
    if (typeof value === "object") {
      for (const key of Object.keys(value)) {
        if ((value as ClassRecord)[key]) out.push(key);
      }
      return;
    }
    out.push(String(value));
  };
  values.forEach(push);
  return out.join(" ");
}

/** 截断长文本，保留可读性（用于路径 / ID 展示） */
export function truncateMiddle(text: string, head = 14, tail = 10): string {
  if (text.length <= head + tail + 1) return text;
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

/** 把任意值安全转成数字，非法时回退 */
export function toNumber(value: unknown, fallback = 0): number {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : fallback;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
