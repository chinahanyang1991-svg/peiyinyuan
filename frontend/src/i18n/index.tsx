/**
 * 轻量 i18n 运行时（无第三方依赖）
 *
 * 能力：
 *  - LOCALES：语言注册表，新增语言只需加一个词典 + 一条注册项；
 *  - t / tOr / tList / tEnum：点号路径取值 + {name} 插值；
 *  - 缺键处理：先回退 zh，再回退调用方给的 fallback（tOr），最后回退键名本身，
 *    绝不渲染空白；
 *  - I18nProvider / useI18n：与 prefsStore 联动，切换语言即时生效并持久化；
 *  - <html lang> 与 Intl 数字/日期格式化随语言切换。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";

import en from "./en";
import zh, { type MessageKey, type Messages } from "./zh";
import type { Interpolation, LocaleDef } from "./types";
import { usePrefsStore, type LocaleCode } from "../store/prefsStore";

export type { Interpolation, LocaleDef, MessageKey, LocaleCode };

export const messages: Record<LocaleCode, Messages> = {
  zh: zh as unknown as Messages,
  en: en as unknown as Messages,
};

export const DEFAULT_LOCALE: LocaleCode = "zh";

/** 语言注册表：下拉/切换按钮据此渲染，新增语言在此追加即可 */
export const LOCALES: Array<{ code: LocaleCode } & LocaleDef> = [
  { code: "zh", tag: "zh-CN", short: "中", nativeName: "简体中文" },
  { code: "en", tag: "en-US", short: "EN", nativeName: "English" },
];

export function localeTagOf(code: LocaleCode): string {
  return LOCALES.find((item) => item.code === code)?.tag ?? "zh-CN";
}

function readPath(root: unknown, path: string): unknown {
  let cursor: unknown = root;
  for (const segment of path.split(".")) {
    if (cursor === null || typeof cursor !== "object") return undefined;
    cursor = (cursor as Record<string, unknown>)[segment];
  }
  return cursor;
}

function interpolate(template: string, vars?: Interpolation): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match
  );
}

/** 核心取词：当前语言 → zh 兜底 → 键名兜底 */
function lookup(code: LocaleCode, key: string, vars?: Interpolation): string {
  const raw = readPath(messages[code], key) ?? readPath(messages[DEFAULT_LOCALE], key);
  if (typeof raw === "string") return interpolate(raw, vars);
  if (Array.isArray(raw)) return interpolate(String(raw[0]), vars);
  return key;
}

function lookupList(code: LocaleCode, key: string): readonly string[] {
  const raw = readPath(messages[code], key) ?? readPath(messages[DEFAULT_LOCALE], key);
  if (Array.isArray(raw)) return raw as readonly string[];
  if (typeof raw === "string") return [raw];
  return [];
}

export type Translate = (key: MessageKey, vars?: Interpolation) => string;
export type TranslateOr = (
  key: MessageKey | undefined | null,
  fallback: string,
  vars?: Interpolation
) => string;
export type TranslateList = (key: MessageKey) => readonly string[];
/** 后端机器码 → 文案；枚举组内查不到时返回原始值（避免空白/undefined） */
export type TranslateEnum = (
  group: keyof Messages["enums"] | string,
  value: string | null | undefined,
  fallback?: string
) => string;

export interface I18nValue {
  locale: LocaleCode;
  localeTag: string;
  setLocale: (code: LocaleCode) => void;
  locales: typeof LOCALES;
  t: Translate;
  tOr: TranslateOr;
  tList: TranslateList;
  tEnum: TranslateEnum;
  /** 当前语言下的整本词典（少数需要动态拼接键的场景使用） */
  dict: Messages;
  format: Formatters;
}

export interface Formatters {
  number: (value: number, options?: Intl.NumberFormatOptions) => string;
  decimal: (value: number, digits?: number) => string;
  percent: (ratio: number, digits?: number) => string;
  dateTime: (value: string | number | Date | null | undefined) => string;
  date: (value: string | number | Date | null | undefined) => string;
  time: (value: string | number | Date | null | undefined) => string;
  bytes: (value: number | null | undefined) => string;
  seconds: (value: number | null | undefined) => string;
}

function createFormatters(tag: string): Formatters {
  const nf = new Intl.NumberFormat(tag);
  const toInput = (value: string | number | Date | null | undefined): Date | null => {
    if (value === null || value === undefined || value === "") return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (typeof value === "number") {
      const fromMs = new Date(value);
      return Number.isNaN(fromMs.getTime()) ? null : fromMs;
    }
    const parsed = new Date(String(value));
    if (!Number.isNaN(parsed.getTime())) return parsed;
    // 后端常见 "2024-01-02 03:04:05"（缺 T / 时区），Safari 需要手工补
    const normalized = new Date(String(value).replace(" ", "T"));
    return Number.isNaN(normalized.getTime()) ? null : normalized;
  };

  const dateTimeFmt = new Intl.DateTimeFormat(tag, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const dateFmt = new Intl.DateTimeFormat(tag, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const timeFmt = new Intl.DateTimeFormat(tag, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  return {
    number: (value, options) =>
      new Intl.NumberFormat(tag, options).format(Number.isFinite(value) ? value : 0),
    decimal: (value, digits = 1) =>
      new Intl.NumberFormat(tag, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }).format(Number.isFinite(value) ? value : 0),
    percent: (ratio, digits = 0) =>
      new Intl.NumberFormat(tag, {
        style: "percent",
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }).format(Number.isFinite(ratio) ? ratio : 0),
    dateTime: (value) => {
      const date = toInput(value);
      return date ? dateTimeFmt.format(date) : "—";
    },
    date: (value) => {
      const date = toInput(value);
      return date ? dateFmt.format(date) : "—";
    },
    time: (value) => {
      const date = toInput(value);
      return date ? timeFmt.format(date) : "—";
    },
    bytes: (value) => {
      const size = Number(value);
      if (!Number.isFinite(size) || size <= 0) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      const exp = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
      const scaled = size / Math.pow(1024, exp);
      return `${new Intl.NumberFormat(tag, { maximumFractionDigits: exp === 0 ? 0 : 1 }).format(scaled)} ${units[exp]}`;
    },
    seconds: (value) => {
      const total = Number(value);
      if (!Number.isFinite(total) || total <= 0) return nf.format(0) + "s";
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = Math.floor(total % 60);
      if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
      if (m > 0) return `${m}:${String(s).padStart(2, "0")}`;
      return `${s}s`;
    },
  };
}

const I18nContext = createContext<I18nValue | null>(null);

export interface I18nProviderProps {
  children: ReactNode;
  /** 不传则跟随 prefsStore（默认行为，语言写进 localStorage） */
  locale?: LocaleCode;
  onLocaleChange?: (code: LocaleCode) => void;
}

export function I18nProvider({ children, locale, onLocaleChange }: I18nProviderProps) {
  const storedLocale = usePrefsStore((state) => state.locale);
  const setStoredLocale = usePrefsStore((state) => state.setLocale);

  const activeLocale: LocaleCode = locale ?? storedLocale ?? DEFAULT_LOCALE;
  const tag = localeTagOf(activeLocale);

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.setAttribute("lang", tag);
    document.documentElement.setAttribute("data-locale", activeLocale);
  }, [tag, activeLocale]);

  const setLocale = useCallback(
    (code: LocaleCode) => {
      onLocaleChange?.(code);
      if (locale === undefined) setStoredLocale(code);
    },
    [locale, onLocaleChange, setStoredLocale]
  );

  const value = useMemo<I18nValue>(() => {
    const t: Translate = (key, vars) => lookup(activeLocale, key as string, vars);
    const tOr: TranslateOr = (key, fallback, vars) => {
      if (!key) return fallback;
      const resolved = lookup(activeLocale, key as string, vars);
      return resolved === (key as string) ? fallback : resolved;
    };
    const tList: TranslateList = (key) => lookupList(activeLocale, key as string);
    const tEnum: TranslateEnum = (group, value, fallback) => {
      if (value === null || value === undefined || value === "") {
        return fallback ?? "";
      }
      const key = `enums.${String(group)}.${value}`;
      const raw =
        readPath(messages[activeLocale], key) ?? readPath(messages[DEFAULT_LOCALE], key);
      if (typeof raw === "string") return raw;
      return fallback ?? String(value);
    };

    return {
      locale: activeLocale,
      localeTag: tag,
      setLocale,
      locales: LOCALES,
      t,
      tOr,
      tList,
      tEnum,
      dict: messages[activeLocale],
      format: createFormatters(tag),
    };
  }, [activeLocale, tag, setLocale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    throw new Error("useI18n 必须在 <I18nProvider> 内使用");
  }
  return ctx;
}

/** 只要翻译函数，方便在纯展示组件里 `const { t } = useT()` */
export function useT(): Translate {
  return useI18n().t;
}
