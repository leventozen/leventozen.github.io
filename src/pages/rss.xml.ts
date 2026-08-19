import rss from "@astrojs/rss";
import { blog } from "../lib/markdoc/frontmatter.schema";
import { readAll } from "../lib/markdoc/read";
import { renderMarkdownToRssHtml } from "../lib/markdoc/render-rss-html";
import {
  SITE_TITLE,
  SITE_DESCRIPTION,
  SITE_URL,
  MY_NAME,
} from "../config";

export const get = async () => {
  const posts = await readAll({
    directory: "blog",
    frontmatterSchema: blog,
  });

  const sortedPosts = posts
    .filter((p) => p.frontmatter.draft !== true)
    .filter((p) => p.frontmatter.external !== true)
    .sort(
      (a, b) =>
        new Date(b.frontmatter.date).valueOf() -
        new Date(a.frontmatter.date).valueOf()
    );

  let baseUrl = SITE_URL;
  baseUrl = baseUrl.replace(/\/+$/g, "");

  const rssItems = sortedPosts.map(({ frontmatter, slug, rawContent }) => {
    const title = frontmatter.title;
    const pubDate = frontmatter.date;
    const description = frontmatter.description ?? frontmatter.title;
    const link = `${baseUrl}/blog/${slug}/`;
    const content = renderMarkdownToRssHtml(rawContent);

    return {
      title,
      pubDate,
      description,
      link,
      content,
      author: MY_NAME,
    };
  });

  const lastBuildDate =
    sortedPosts.length > 0
      ? sortedPosts[0].frontmatter.date
      : new Date();

  return rss({
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    site: baseUrl,
    items: rssItems,
    customData: `<language>en-us</language><lastBuildDate>${lastBuildDate.toUTCString()}</lastBuildDate><managingEditor>${MY_NAME}</managingEditor>`,
  });
};
