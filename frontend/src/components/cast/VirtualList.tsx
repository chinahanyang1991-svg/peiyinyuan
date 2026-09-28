/**
 * VirtualList —— 轻量窗口化列表（批次 B 自带，不引第三方依赖）
 *
 * 为什么需要：多人有声书一本书能析出几十上百个角色，BGM/音效库和角色增益表也会很长；
 * 每张角色卡内含下拉/滑杆等受控子节点，全量渲染在滚动时会明显掉帧。
 *
 * 实现要点（性能红线）：
 * - 只渲染视口内 + overscan 的行（卡片按 columns 折叠成"行"，行高固定）；
 * - 滚动监听用 rAF 节流，setState 只在窗口区间变化时触发；
 * - ResizeObserver 测宽自适应列数，避免手写断点；
 * - 条目数 <= 一屏行数时退化为普通列表（零额外 DOM）。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export interface VirtualListProps<T> {
  items: T[];
  /** 单行高度（含间距），必须与 renderItem 的卡片实际占位一致 */
  rowHeight: number;
  /** 视口高度 */
  height: number;
  /** 每行卡片数；不传则按 minColumnWidth 自适应 */
  columns?: number;
  /** 自适应列数的最小卡宽（columns 未传时生效） */
  minColumnWidth?: number;
  /** 上下各多渲染几行，防止快速滚动白块 */
  overscan?: number;
  keyOf: (item: T, index: number) => string | number;
  renderItem: (item: T, index: number) => React.ReactNode;
  className?: string;
  empty?: React.ReactNode;
}

export default function VirtualList<T>({
  items, rowHeight, height, columns, minColumnWidth = 280, overscan = 2,
  keyOf, renderItem, className = '', empty,
}: VirtualListProps<T>) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [measuredWidth, setMeasuredWidth] = useState(0);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    const node = boxRef.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(entries => {
      for (const entry of entries) setMeasuredWidth(entry.contentRect.width);
    });
    ro.observe(node);
    setMeasuredWidth(node.clientWidth);
    return () => ro.disconnect();
  }, []);

  const resolvedColumns = useMemo(() => {
    if (columns && columns > 0) return columns;
    if (!measuredWidth) return 1;
    return Math.max(1, Math.floor((measuredWidth + 12) / (minColumnWidth + 12)));
  }, [columns, measuredWidth, minColumnWidth]);

  const rowCount = Math.ceil(items.length / resolvedColumns);
  const visibleRows = Math.max(1, Math.ceil(height / rowHeight));
  const startRow = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const endRow = Math.min(rowCount, Math.floor(scrollTop / rowHeight) + visibleRows + overscan);
  const virtualized = rowCount > visibleRows + overscan * 2;

  const onScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const top = e.currentTarget.scrollTop;
    if (frameRef.current !== null) return;
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = null;
      setScrollTop(prev => (Math.abs(prev - top) >= rowHeight / 2 ? top : prev));
    });
  }, [rowHeight]);

  useEffect(() => () => {
    if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
  }, []);

  if (items.length === 0) {
    return <div className={`cast-virtual ${className}`} ref={boxRef} style={{ height }}>{empty ?? null}</div>;
  }

  const rows: React.ReactNode[] = [];
  const from = virtualized ? startRow : 0;
  const to = virtualized ? endRow : rowCount;
  for (let r = from; r < to; r += 1) {
    const cells: React.ReactNode[] = [];
    for (let c = 0; c < resolvedColumns; c += 1) {
      const idx = r * resolvedColumns + c;
      if (idx >= items.length) break;
      const item = items[idx];
      cells.push(
        <div className="cast-virtual-cell" key={`c-${keyOf(item, idx)}`}>
          {renderItem(item, idx)}
        </div>,
      );
    }
    rows.push(
      <div
        className="cast-virtual-row"
        key={`r-${r}`}
        style={{ height: rowHeight, gridTemplateColumns: `repeat(${resolvedColumns}, minmax(0, 1fr))` }}
      >
        {cells}
      </div>,
    );
  }

  return (
    <div className={`cast-virtual ${className}`} ref={boxRef} onScroll={onScroll} style={{ height }}>
      <div style={{ height: rowCount * rowHeight, position: 'relative' }}>
        <div style={{ transform: `translateY(${from * rowHeight}px)` }}>{rows}</div>
      </div>
    </div>
  );
}
