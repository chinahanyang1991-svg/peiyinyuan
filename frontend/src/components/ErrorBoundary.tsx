import { Component, type ErrorInfo, type ReactNode } from 'react';
import { TriangleAlert } from 'lucide-react';
import { useI18n } from '../i18n';
import { Button } from './ui';

interface Props {
  children: ReactNode;
  /**
   * 内联模式：只渲染内容区那张卡，不再自带 `.shell`/`.main` 外框。
   * 路由级兜底必须走这条，否则外壳里再套一层外壳（导航栏、页脚会重复渲染）。
   */
  inline?: boolean;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/** 崩溃兜底页：走设计系统样式，文案全部来自词典 */
function ErrorScreen({ message, inline = false }: { message?: string; inline?: boolean }) {
  const { t } = useI18n();
  const card = (
    <div className="card card--pad" style={{ textAlign: 'center', padding: 'var(--space-10)' }}>
      <div
        style={{
          display: 'grid',
          placeContent: 'center',
          width: '3.5rem',
          height: '3.5rem',
          margin: '0 auto var(--space-4)',
          borderRadius: 'var(--radius-lg)',
          background: 'var(--danger-soft)',
          color: 'var(--danger-text)',
        }}
      >
        <TriangleAlert size={22} aria-hidden="true" />
      </div>
      <h1 className="page-title" style={{ fontSize: 'var(--text-2xl)' }}>
        {t('app.errorTitle')}
      </h1>
      <p className="empty__desc" style={{ margin: 'var(--space-2) auto', maxWidth: '46ch' }}>
        {t('app.errorDesc')}
      </p>
      {message ? (
        <p className="hint u-mono" style={{ marginTop: 'var(--space-3)' }}>
          {message}
        </p>
      ) : null}
      <div className="btn-group" style={{ justifyContent: 'center', marginTop: 'var(--space-5)' }}>
        <Button
          variant="primary"
          onClick={() => {
            window.location.reload();
          }}
        >
          {t('app.reload')}
        </Button>
      </div>
    </div>
  );
  if (inline) {
    return <div className="page">{card}</div>;
  }
  return (
    <div className="shell" data-collapsed="false">
      <div className="main" style={{ marginLeft: 0 }}>
        <div className="page">{card}</div>
      </div>
    </div>
  );
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('ErrorBoundary caught:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return <ErrorScreen message={this.state.error?.message} inline={this.props.inline} />;
    }
    return this.props.children;
  }
}
