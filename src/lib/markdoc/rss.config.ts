import Markdoc from "@markdoc/markdoc";
import type { Config } from "@markdoc/markdoc";

const { nodes, Tag } = Markdoc;

function embedLinkTransform(
  node: Parameters<NonNullable<Config["tags"]>[string]["transform"]>[0],
  config: Parameters<NonNullable<Config["tags"]>[string]["transform"]>[1],
  fallbackLabel: string
) {
  const attributes = node.transformAttributes(config);
  const url = attributes.url as string;
  const label = (attributes.label as string | undefined) || fallbackLabel;

  return new Tag("p", {}, [new Tag("a", { href: url }, [label])]);
}

export const rssConfig: Config = {
  tags: {
    details: {
      render: "details",
      children: nodes.document.children,
    },
    summary: {
      render: "summary",
      children: nodes.document.children,
    },
    sup: {
      render: "sup",
      children: nodes.strong.children,
    },
    sub: {
      render: "sub",
      children: nodes.strong.children,
    },
    abbr: {
      render: "abbr",
      attributes: {
        title: { type: String },
      },
      children: nodes.strong.children,
    },
    kbd: {
      render: "kbd",
      children: nodes.strong.children,
    },
    mark: {
      render: "mark",
      children: nodes.strong.children,
    },
    youtube: {
      render: "p",
      attributes: {
        url: { type: String, required: true },
        label: { type: String, required: true },
      },
      selfClosing: true,
      transform(node, config) {
        return embedLinkTransform(node, config, "Watch on YouTube");
      },
    },
    tweet: {
      render: "p",
      attributes: {
        url: { type: String, required: true },
      },
      selfClosing: true,
      transform(node, config) {
        return embedLinkTransform(node, config, "View on Twitter");
      },
    },
    codepen: {
      render: "p",
      attributes: {
        url: { type: String, required: true },
        title: { type: String, required: true },
      },
      selfClosing: true,
      transform(node, config) {
        const attributes = node.transformAttributes(config);
        const url = attributes.url as string;
        const title = attributes.title as string;
        return new Tag("p", {}, [new Tag("a", { href: url }, [title])]);
      },
    },
    githubgist: {
      render: "p",
      attributes: {
        id: { type: String, required: true },
      },
      selfClosing: true,
      transform(node, config) {
        const attributes = node.transformAttributes(config);
        const id = attributes.id as string;
        const url = `https://gist.github.com/${id}`;
        return new Tag("p", {}, [new Tag("a", { href: url }, [`GitHub Gist: ${id}`])]);
      },
    },
  },
  nodes: {
    heading: {
      render: "h1",
      attributes: {
        level: { type: Number, required: true },
      },
      transform(node, config) {
        const attributes = node.transformAttributes(config);
        const children = node.transformChildren(config);
        const level = attributes.level as number;
        return new Tag(`h${level}`, {}, children);
      },
    },
    fence: {
      render: "pre",
      attributes: {
        content: { type: String, render: false, required: true },
        language: { type: String, default: "typescript" },
        process: { type: Boolean, render: false, default: false },
      },
      transform(node, config) {
        const attributes = node.transformAttributes(config);
        const children = node.transformChildren(config);
        if (children.some((child) => typeof child !== "string")) {
          throw new Error(
            `unexpected non-string child of code block from ${
              node.location?.file ?? "(unknown file)"
            }:${node.location?.start.line ?? "(unknown line)"}`
          );
        }
        const content = children.join("");
        const language = attributes.language as string;
        return new Tag(
          "pre",
          {},
          [new Tag("code", { class: `language-${language}` }, [content])]
        );
      },
    },
  },
};
