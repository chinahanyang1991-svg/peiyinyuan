/**
 * WebSocket hook for real-time task progress updates.
 * Used by ConvertPage to receive live progress from the backend.
 */

import { useEffect, useRef, useState, useCallback } from 'react';

interface ProgressData {
  task_id: string;
  status: string;
  progress: number;
  current_stage: string;
  processed_chars: number;
  total_chars: number;
  error_message?: string;
  elapsed_seconds?: number;
}

interface UseWebSocketOptions {
  onMessage?: (data: ProgressData) => void;
  onError?: (error: Event) => void;
  onClose?: () => void;
  enabled?: boolean;
}

export function useWebSocket(taskId: string | undefined, options: UseWebSocketOptions = {}) {
  const { onMessage, onError, onClose, enabled = true } = options;
  const [data, setData] = useState<ProgressData | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);

  const connect = useCallback(() => {
    if (!taskId || !enabled) return;

    // Determine WebSocket URL from current origin
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/tasks/${taskId}/progress`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setError(null);
      };

      ws.onmessage = (event) => {
        try {
          const progressData: ProgressData = JSON.parse(event.data);
          setData(progressData);
          onMessage?.(progressData);

          // Auto-close if task is in terminal state
          if (['completed', 'failed', 'cancelled'].includes(progressData.status)) {
            ws.close();
          }
        } catch (e) {
          console.error('WebSocket message parse error:', e);
        }
      };

      ws.onerror = (event) => {
        setError('WebSocket connection error');
        onError?.(event);
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        onClose?.();
      };
    } catch (e) {
      setError('Failed to create WebSocket connection');
    }
  }, [taskId, enabled, onMessage, onError, onClose]);

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setConnected(false);
  }, []);

  useEffect(() => {
    connect();

    return () => {
      disconnect();
    };
  }, [connect, disconnect]);

  return {
    data,
    connected,
    error,
    disconnect,
    reconnect: connect,
  };
}