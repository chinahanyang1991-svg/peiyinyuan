import { useCallback, useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  AudioLines,
  Clapperboard,
  FileUp,
  Gauge,
  History,
  LayoutDashboard,
  Library,
  Menu,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  PenLine,
  Sun,
  Workflow,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { systemApi } from "../services/api";
import { modeOfTier, useAppStore } from "../store/appStore";
import { usePrefsStore } from "../store/prefsStore";
import { useI18n } from "../i18n";
import type { MessageKey } from "../i18n";
import { LoaderBlock } from "./ui";
import ErrorBoundary from "./ErrorBoundary";
import { cx } from "../lib/cx";

interface NavItem {
  to: string;
  labelKey: MessageKey;
  icon: LucideIcon;
  /** 前缀完全匹配（首页需要 end，其余章节用前缀高亮） */
  end?: boolean;
}

interface NavGroup {
  labelKey: MessageKey;
  items: NavItem[];
}

/** 导航结构与现有路由一一对应，path 值保持不变 */
const NAV_GROUPS: NavGroup[] = [
  {
    labelKey: "nav.groupWorkflow",
    items: [
      { to: "/", labelKey: "nav.home", icon: LayoutDashboard, end: true },
      { to: "/import", labelKey: "nav.import", icon: FileUp },
      { to: "/edit", labelKey: "nav.edit", icon: PenLine },
      { to: "/dubbing", labelKey: "nav.dubbing", icon: Clapperboard },
      { to: "/convert", labelKey: "nav.convert", icon: Gauge },
      { to: "/library", labelKey: "nav.library", icon: Library },
    ],
  },
  {
    labelKey: "nav.groupAssets",
    items: [{ to: "/voices", labelKey: "nav.voices", icon: AudioLines }],
  },
  {
    labelKey: "nav.groupSystem",
    items: [
      { to: "/memory", labelKey: "nav.memory", icon: History },
      { to: "/workflows", labelKey: "nav.workflows", icon: Workflow },
    ],
  },
];

const ALL_ITEMS: NavItem[] = NAV_GROUPS.flatMap((group) => group.items);

function useActiveNavItem(pathname: string): NavItem | undefined {
  return useMemo(() => {
    if (pathname === "/") return ALL_ITEMS[0];
    const matches = ALL_ITEMS.filter((item) => item.to !== "/" && pathname.startsWith(item.to));
    if (matches.length === 0) return undefined;
    return matches.reduce((a, b) => (a.to.length >= b.to.length ? a : b));
  }, [pathname]);
}

export default function AppShell() {
  const { t, locale, setLocale, locales } = useI18n();
  const location = useLocation();
  const theme = usePrefsStore((state) => state.theme);
  const setTheme = usePrefsStore((state) => state.setTheme);
  const collapsed = usePrefsStore((state) => state.sidebarCollapsed);
  const toggleSidebar = usePrefsStore((state) => state.toggleSidebar);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const backendReady = useAppStore((state) => state.backendReady);
  const gpuAvailable = useAppStore((state) => state.gpuAvailable);
  const ttsEngine = useAppStore((state) => state.ttsEngine);
  const ttsModes = useAppStore((state) => state.ttsModes);
  const setBackendReady = useAppStore((state) => state.setBackendReady);
  const setSystemInfo = useAppStore((state) => state.setSystemInfo);
  const setTtsModes = useAppStore((state) => state.setTtsModes);
  const chosenTier = usePrefsStore((state) => state.ttsTier);

  const activeItem = useActiveNavItem(location.pathname);

  /** 换页后自动收起移动端抽屉，避免遮挡内容 */
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  const probeBackend = useCallback(() => {
    systemApi
      .health()
      .then((response) => {
        const data = response?.data ?? {};
        setBackendReady(data.status === "ok");
        if (typeof data.tts_engine === "string" || typeof data.gpu_available === "boolean") {
          setSystemInfo({
            gpu_available: Boolean(data.gpu_available),
            tts_engine: String(data.tts_engine ?? ""),
            tts_tier: String(data.tts_tier ?? ""),
          });
        }
      })
      .catch(() => setBackendReady(false));
  }, [setBackendReady, setSystemInfo]);

  /** 两档模式的元数据（引擎名/可用性）：静态配置，开机问一次，不进 30s 轮询。 */
  const probeEngines = useCallback(() => {
    systemApi
      .engines()
      .then((response) => {
        const data = response?.data ?? {};
        if (data.status !== "ok" || !Array.isArray(data.modes)) return;
        setTtsModes(data.modes);
      })
      .catch(() => {
        // 保持 ttsModes=[] —— 开关据此显示"未取到模式信息"，不拿猜测值糊上去
      });
  }, [setTtsModes]);

  useEffect(() => {
    probeBackend();
    probeEngines();
    const timer = window.setInterval(probeBackend, 30000);
    return () => window.clearInterval(timer);
  }, [probeBackend, probeEngines]);

  /** 页脚报的是"这个模式真会用的那台引擎"，不是服务端配置里恰好写着的那台。 */
  const footerEngine =
    modeOfTier(ttsModes, chosenTier)?.engine_display || ttsEngine || "-";

  const sidebar = (
    <aside className="sidebar" aria-label={t("app.name")}>
      <div className="sidebar__brand">
        <span className="sidebar__logo">
          <AudioLines size={15} aria-hidden="true" />
        </span>
        <span className="sidebar__wordmark">
          <span className="sidebar__name">{t("app.name")}</span>
          <span className="sidebar__edition">{t("app.edition")}</span>
        </span>
      </div>

      <div className="sidebar__scroll">
        {NAV_GROUPS.map((group) => (
          <div className="sidebar__group" key={group.labelKey}>
            <p className="sidebar__group-label">{t(group.labelKey)}</p>
            <nav>
              {group.items.map((item) => {
                const Icon = item.icon;
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) => cx("nav-item", isActive && "is-active")}
                    title={t(item.labelKey)}
                  >
                    <Icon size={16} className="nav-item__icon" aria-hidden="true" />
                    <span className="nav-item__label">{t(item.labelKey)}</span>
                  </NavLink>
                );
              })}
            </nav>
          </div>
        ))}
      </div>

      <div className="sidebar__foot">
        <div
          className="sys-status"
          data-state={backendReady ? "online" : "offline"}
          title={backendReady ? t("app.backendOnline") : t("app.backendOffline")}
        >
          <span className="sys-status__dot" aria-hidden="true" />
          <span className="sys-status__text">
            {backendReady ? t("app.backendOnline") : t("app.backendOffline")}
          </span>
        </div>
        <button
          type="button"
          className="sidebar__collapse"
          onClick={toggleSidebar}
          aria-label={collapsed ? t("nav.expand") : t("nav.collapse")}
          aria-pressed={collapsed}
        >
          {collapsed ? (
            <PanelLeftOpen size={16} aria-hidden="true" />
          ) : (
            <PanelLeftClose size={16} aria-hidden="true" />
          )}
          <span className="sidebar__collapse-text">
            {collapsed ? t("nav.expand") : t("nav.collapse")}
          </span>
        </button>
      </div>
    </aside>
  );

  return (
    <div
      className="shell"
      data-collapsed={collapsed ? "true" : "false"}
      data-drawer={drawerOpen ? "open" : "closed"}
    >
      <a className="skip-link" href="#main">
        {t("app.skipToContent")}
      </a>

      {sidebar}

      {drawerOpen ? (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label={t("nav.closeMenu")}
          onClick={() => setDrawerOpen(false)}
        />
      ) : null}

      <div className="main">
        <header className="topbar">
          <button
            type="button"
            className="icon-btn topbar__menu"
            onClick={() => setDrawerOpen((open) => !open)}
            aria-label={drawerOpen ? t("nav.closeMenu") : t("nav.openMenu")}
            aria-expanded={drawerOpen}
          >
            {drawerOpen ? <X size={18} aria-hidden="true" /> : <Menu size={18} aria-hidden="true" />}
          </button>

          <div className="topbar__crumbs">
            <span className="topbar__crumb">{t("app.name")}</span>
            {activeItem ? (
              <>
                <span className="topbar__sep" aria-hidden="true">
                  /
                </span>
                <span className="topbar__crumb topbar__crumb--current">
                  {t(activeItem.labelKey)}
                </span>
              </>
            ) : null}
          </div>

          <span className="topbar__spacer" />

          <div className="topbar__tools">
            <div className="lang-switch" role="group" aria-label={t("lang.switchTo")}>
              {locales.map((item) => (
                <button
                  key={item.code}
                  type="button"
                  className={cx("lang-switch__btn", item.code === locale && "is-active")}
                  onClick={() => setLocale(item.code)}
                  aria-pressed={item.code === locale}
                  title={item.nativeName}
                >
                  {item.short}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="icon-btn"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              aria-label={theme === "dark" ? t("theme.toggleToLight") : t("theme.toggleToDark")}
              title={theme === "dark" ? t("theme.light") : t("theme.dark")}
            >
              {theme === "dark" ? <Sun size={18} aria-hidden="true" /> : <Moon size={18} aria-hidden="true" />}
            </button>
          </div>
        </header>

        <div className="main__content" id="main" tabIndex={-1}>
          {/* key=pathname：崩了的页面靠"换路由"就能自愈，不必刷新整站 */}
          <ErrorBoundary key={location.pathname} inline>
            <Outlet />
          </ErrorBoundary>
        </div>

        <footer className="app-footer">
          <span>{t("footer.hint")}</span>
          <span className="u-row u-center u-gap-3 u-wrap">
            <span>{t("footer.status", { state: backendReady ? t("app.online") : t("app.offline") })}</span>
            <span aria-hidden="true">·</span>
            <span>{t("footer.engine", { engine: footerEngine })}</span>
            <span aria-hidden="true">·</span>
            <span>{t("footer.device", { device: gpuAvailable ? "GPU" : "CPU" })}</span>
          </span>
        </footer>
      </div>
    </div>
  );
}

/** Suspense fallback：与新外壳同构，避免懒加载时整页跳动 */
export function ShellLoading() {
  const { t } = useI18n();
  return (
    <div className="shell" data-collapsed="false">
      <div className="main" style={{ marginLeft: 0, minHeight: "60vh" }}>
        <LoaderBlock>{t("app.loading")}</LoaderBlock>
      </div>
    </div>
  );
}
