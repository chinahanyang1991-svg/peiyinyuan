import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { I18nProvider } from './i18n';
import { ConfirmProvider, ToastProvider } from './components/ui';
import { syncPrefsToDom } from './store/prefsStore';
// 过渡层：旧页面尚未全部迁移完，Phase 5 收尾时连同 global.css 一并删除
import './styles/global.css';
import './styles/index.css';

/** 首屏就把已持久化的主题写进 DOM，避免浅色闪烁 */
syncPrefsToDom();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <I18nProvider>
        <ToastProvider>
          <ConfirmProvider>
            <App />
          </ConfirmProvider>
        </ToastProvider>
      </I18nProvider>
    </BrowserRouter>
  </React.StrictMode>
);
