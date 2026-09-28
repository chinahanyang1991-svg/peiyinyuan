/**
 * Zustand 全局状态管理
 */

import { create } from 'zustand';

/** 配音模式（档位）：在线 = 不发本地权重，本地 = 加载本地大模型。 */
export type TtsTier = 'online' | 'local';

/** `GET /api/system/engines` 的 modes 元素（后端 modules/tts_engine/tier.py:describe）。 */
export interface TtsModeInfo {
  tier: string;
  engine: string;
  engine_display: string;
  available: boolean;
  reason?: string | null;
  online: boolean;
  unverified_cloud?: boolean;
}

interface AppState {
  // 系统状态
  backendReady: boolean;
  gpuAvailable: boolean;
  ttsEngine: string;
  /** 服务端配置的默认档位（用户自己的选择见 prefsStore.ttsTier） */
  ttsTier: TtsTier;
  /** 两档模式的元数据；空数组 = 还没问到（渲染时按"未知"降级，不当成"没有"） */
  ttsModes: TtsModeInfo[];

  // 操作状态
  taskCount: number;

  // Actions
  setBackendReady: (ready: boolean) => void;
  setSystemInfo: (info: {
    gpu_available: boolean;
    tts_engine: string;
    tts_tier?: string;
  }) => void;
  setTtsModes: (modes: TtsModeInfo[]) => void;
  incrementTaskCount: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  backendReady: false,
  gpuAvailable: false,
  // 'edgetts' 兜底：这是当前在线档默认引擎，健康检查到达前也不该显示成"本地大模型"
  ttsEngine: 'edgetts',
  ttsTier: 'online',
  ttsModes: [],
  taskCount: 0,

  setBackendReady: (ready) => set({ backendReady: ready }),
  setSystemInfo: (info) => set({
    gpuAvailable: info.gpu_available,
    ttsEngine: info.tts_engine,
    ttsTier: info.tts_tier === 'local' ? 'local' : 'online',
  }),
  setTtsModes: (modes) => set({ ttsModes: modes }),
  incrementTaskCount: () => set((state) => ({ taskCount: state.taskCount + 1 })),
}));

/** 某档位当前的默认嗓；没问到元数据时返回 null（调用方按"未知"渲染，不许猜）。 */
export function modeOfTier(modes: TtsModeInfo[], tier: string): TtsModeInfo | null {
  return modes.find((mode) => mode.tier === tier) ?? null;
}

/** 后端/本地存储里的档位可能是脏值 —— 只认两个合法值，其余回落 fallback。 */
export function normalizeTier(value: unknown, fallback: TtsTier): TtsTier {
  return value === "online" || value === "local" ? value : fallback;
}
