/**
 * 概览页（UI-100 阶段3 重制）：纯展示 + 导航入口，无后端调用。
 */
import { useNavigate } from "react-router-dom";
import { Archive, FileUp, Radio, SlidersHorizontal, Users, Wand2 } from "lucide-react";
import { useI18n } from "../i18n";
import { Button, Card, Page, SectionTitle } from "../components/ui";

/** 九阶段机器码，与后端 stage 契约一致 */
const STAGES = [
  "import",
  "parse",
  "clean",
  "split",
  "analyze",
  "assign",
  "synthesize",
  "postprocess",
  "package",
] as const;

const FEATURES = [
  { icon: FileUp, titleKey: "home.feature1Title", descKey: "home.feature1Desc" },
  { icon: Radio, titleKey: "home.feature2Title", descKey: "home.feature2Desc" },
  { icon: Users, titleKey: "home.feature3Title", descKey: "home.feature3Desc" },
  { icon: SlidersHorizontal, titleKey: "home.feature4Title", descKey: "home.feature4Desc" },
  { icon: Wand2, titleKey: "home.feature5Title", descKey: "home.feature5Desc" },
  { icon: Archive, titleKey: "home.feature6Title", descKey: "home.feature6Desc" },
] as const;

export default function HomePage() {
  const { t, tEnum } = useI18n();
  const navigate = useNavigate();

  return (
    <Page>
      <section className="hero">
        <p className="hero__eyebrow">{t("home.eyebrow")}</p>
        <h1 className="hero__title">{t("home.title")}</h1>
        <p className="hero__lead">{t("home.lead")}</p>
        <div className="hero__actions">
          <Button variant="primary" size="lg" icon={FileUp} onClick={() => navigate("/import")}>
            {t("home.ctaImport")}
          </Button>
          <Button variant="secondary" size="lg" onClick={() => navigate("/convert")}>
            {t("home.ctaConvert")}
          </Button>
          <Button variant="ghost" size="lg" onClick={() => navigate("/library")}>
            {t("home.ctaLibrary")}
          </Button>
        </div>
      </section>

      <section className="u-mt-8">
        <SectionTitle>{t("home.pipelineTitle")}</SectionTitle>
        <p className="hint u-mt-1">{t("home.pipelineDesc")}</p>
        <ol className="pipeline-overview u-mt-4">
          {STAGES.map((stage, index) => (
            <li className="pipeline-step" key={stage}>
              <span className="pipeline-step__no">{index + 1}</span>
              <span className="pipeline-step__title">{tEnum("stage", stage)}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="u-mt-8">
        <SectionTitle>{t("home.featuresTitle")}</SectionTitle>
        <p className="hint u-mt-1">{t("home.featuresDesc")}</p>
        <div className="card-grid u-mt-4">
          {FEATURES.map((item) => {
            const Icon = item.icon;
            return (
              <Card key={item.titleKey} title={t(item.titleKey)} desc={t(item.descKey)} icon={Icon} />
            );
          })}
        </div>
      </section>
    </Page>
  );
}
