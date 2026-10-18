import fs from "node:fs/promises";
import path from "node:path";
import satori from "satori";
import { Resvg } from "@resvg/resvg-js";
import { readAll } from "../../lib/markdoc/read";
import { blog } from "../../lib/markdoc/frontmatter.schema";

// Default share card for posts without their own `ogImagePath`.
const WIDTH = 1200;
const HEIGHT = 630;

export async function getStaticPaths() {
  const posts = await readAll({ directory: "blog", frontmatterSchema: blog });
  return posts
    .filter(({ frontmatter }) => !frontmatter.draft && !frontmatter.external)
    .map((post) => ({
      params: { slug: post.slug },
      props: {
        title: post.frontmatter.title,
        date: post.frontmatter.date,
      },
    }));
}

const fontDir = path.join(process.cwd(), "public", "fonts");

export async function get({ props }: { props: { title: string; date: Date } }) {
  const [regular, bold] = await Promise.all([
    fs.readFile(path.join(fontDir, "open-sans-v34-latin-regular.woff")),
    fs.readFile(path.join(fontDir, "open-sans-v34-latin-700.woff")),
  ]);
  const date = props.date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });

  const svg = await satori(
    {
      type: "div",
      props: {
        style: {
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "72px 80px",
          background: "#0f172a",
          color: "#f8fafc",
          fontFamily: "Open Sans",
        },
        children: [
          {
            type: "div",
            props: {
              style: { fontSize: 56, fontWeight: 700, lineHeight: 1.2 },
              children: props.title,
            },
          },
          {
            type: "div",
            props: {
              style: {
                display: "flex",
                justifyContent: "space-between",
                fontSize: 28,
                color: "#94a3b8",
              },
              children: [
                { type: "div", props: { children: date } },
                { type: "div", props: { children: "levent.dev" } },
              ],
            },
          },
        ],
      },
    } as any,
    {
      width: WIDTH,
      height: HEIGHT,
      fonts: [
        { name: "Open Sans", data: regular, weight: 400, style: "normal" },
        { name: "Open Sans", data: bold, weight: 700, style: "normal" },
      ],
    }
  );

  const png = new Resvg(svg, { fitTo: { mode: "width", value: WIDTH } })
    .render()
    .asPng();
  return { body: Buffer.from(png).toString("binary"), encoding: "binary" };
}
