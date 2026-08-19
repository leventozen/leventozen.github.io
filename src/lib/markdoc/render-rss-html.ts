import Markdoc from "@markdoc/markdoc";
import { rssConfig } from "./rss.config";
import { config as siteConfig } from "./markdoc.config";

export function renderMarkdownToRssHtml(markdown: string): string {
  const ast = Markdoc.parse(markdown);

  const errors = Markdoc.validate(ast, siteConfig);
  if (errors.length) {
    throw new Error(
      `Markdoc validation error: ${errors.map((e) => e.message).join(", ")}`
    );
  }

  const content = Markdoc.transform(ast, rssConfig);
  return Markdoc.renderers.html(content);
}
