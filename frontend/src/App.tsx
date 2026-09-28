import { Suspense, lazy } from 'react';
import { Routes, Route } from 'react-router-dom';
import AppShell, { ShellLoading } from './components/AppShell';
import ErrorBoundary from './components/ErrorBoundary';
// 过渡层：尚未迁移的页面仍在用旧动画名，Phase 5 收尾时删除
import './styles/animations.css';

// 懒加载页面组件
const HomePage = lazy(() => import('./routes/HomePage'));
const ImportPage = lazy(() => import('./routes/ImportPage'));
const EditPage = lazy(() => import('./routes/EditPage'));
const VoiceStudioPage = lazy(() => import('./routes/VoiceStudioPage'));
const DubbingPage = lazy(() => import('./routes/DubbingPage'));
const ConvertPage = lazy(() => import('./routes/ConvertPage'));
const LibraryPage = lazy(() => import('./routes/LibraryPage'));
const MemoryPage = lazy(() => import('./routes/MemoryPage'));
const WorkflowPage = lazy(() => import('./routes/WorkflowPage'));
const NotFoundPage = lazy(() => import('./routes/NotFoundPage'));

export default function App() {
  return (
    <ErrorBoundary>
      <Suspense fallback={<ShellLoading />}>
        <Routes>
          {/* 路由 path 全部保持原样，只重做外壳 */}
          <Route element={<AppShell />}>
            <Route path="/" element={<HomePage />} />
            <Route path="/import" element={<ImportPage />} />
            <Route path="/edit/:bookId?" element={<EditPage />} />
            <Route path="/voices" element={<VoiceStudioPage />} />
            <Route path="/dubbing/:projectId?" element={<DubbingPage />} />
            <Route path="/convert/:taskId?" element={<ConvertPage />} />
            <Route path="/library" element={<LibraryPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/workflows" element={<WorkflowPage />} />
            {/* 兜底必须在布局路由之内：未知地址也要有壳、有导航，不能是整页空白 */}
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}
