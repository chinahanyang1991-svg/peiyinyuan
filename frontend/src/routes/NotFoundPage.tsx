/**
 * 未知路由兜底页。
 *
 * 补这一页之前的形状是：地址栏敲一个不存在的.path 时，`<Routes>` 一枚都不匹配，
 * AppShell 照常渲染但内容区是空白 —— 用户看到的"坏了"和"页面还没加载完"长得一样。
 * 路由级 ErrorBoundary（`components/ErrorBoundary.tsx`）管不到这里，因为它只在
 * 子树**抛错**时接管，而"没有匹配的路由"不是异常。
 */
import { Link } from "react-router-dom";
import { Compass } from "lucide-react";
import { useI18n } from "../i18n";
import { Button, EmptyState, Page, PageHeader } from "../components/ui";

export default function NotFoundPage() {
  const { t } = useI18n();
  return (
    <Page>
      <PageHeader title={t("app.notFoundTitle")} />
      <EmptyState
        icon={Compass}
        title={t("app.notFoundTitle")}
        desc={t("app.notFoundDesc")}
        actions={
          <Link to="/">
            <Button variant="primary">{t("app.backHome")}</Button>
          </Link>
        }
      />
    </Page>
  );
}
