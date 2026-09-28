/**
 * 用户偏好（主题 / 语言 / 侧栏折叠 / 配音模式）
 * 单一持久化来源：localStorage["pyy.prefs"]，zustand persist 中间件负责读写。
 * 主题通过 documentElement[data-theme] 驱动 src/styles/tokens.css。
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { TtsTier } from "./appStore";

export type ThemeMode = "light" | "dark";
export type LocaleCode = "zh" | "en";
/** 用户自己选的配音模式（指令#1）。服务端配置的档位是另一件事，见 appStore.ttsTier。 */
export type TtsTierChoice = TtsTier;

export const THEME_ATTR = "data-theme";
export const PREFS_STORAGE_KEY = "pyy.prefs";

interface PrefsState {
  theme: ThemeMode;
  locale: LocaleCode;
  sidebarCollapsed: boolean;
  ttsTier: TtsTierChoice;
  setTheme: (theme: ThemeMode) => void;
  toggleTheme: () => void;
  setLocale: (locale: LocaleCode) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebar: () => void;
  setTtsTier: (tier: TtsTierChoice) => void;
}

function readSystemTheme(): ThemeMode {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** 首屏防闪烁：在 React 挂载前也要能调用（main.tsx 里显式执行一次） */
export function applyThemeToDom(theme: ThemeMode): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute(THEME_ATTR, theme);
}

export const usePrefsStore = create<PrefsState>()(
  persist(
    (set, get) => ({
      theme: readSystemTheme(),
      locale: "zh",
      sidebarCollapsed: false,
      ttsTier: "online",
      setTheme: (theme) => {
        applyThemeToDom(theme);
        set({ theme });
      },
      toggleTheme: () => {
        get().setTheme(get().theme === "dark" ? "light" : "dark");
      },
      setLocale: (locale) => set({ locale }),
      setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
      toggleSidebar: () => set({ sidebarCollapsed: !get().sidebarCollapsed }),
      setTtsTier: (ttsTier) => set({ ttsTier }),
    }),
    {
      name: PREFS_STORAGE_KEY,
      partialize: (state) => ({
        theme: state.theme,
        locale: state.locale,
        sidebarCollapsed: state.sidebarCollapsed,
        ttsTier: state.ttsTier,
      }),
    }
  )
);

/** 把已持久化的偏好立刻写回 DOM（早于首次渲染调用） */
export function syncPrefsToDom(): void {
  const { theme } = usePrefsStore.getState();
  applyThemeToDom(theme);
}
