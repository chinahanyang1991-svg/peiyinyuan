import type { CSSProperties } from "react";

/**
 * 把 CSS 自定义属性（--xxx）安全地塞进 style。
 * React 的 CSSProperties 没有 --* 索引签名，集中在这里做一次转换，
 * 组件里就不用到处写 as any / as unknown as 了。
 */
export function cssVars(values: Record<string, string | number | undefined>): CSSProperties {
  const out: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) out[key.startsWith("--") ? key : `--${key}`] = value;
  }
  return out as unknown as CSSProperties;
}
