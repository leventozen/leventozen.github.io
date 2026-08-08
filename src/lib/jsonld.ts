type JsonLd = Record<string, unknown>;

export function serializeJsonLd(data: JsonLd): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}

export function getHomeJsonLd({
  siteUrl,
  siteTitle,
  siteDescription,
  authorName,
  sameAs,
}: {
  siteUrl: string;
  siteTitle: string;
  siteDescription: string;
  authorName: string;
  sameAs: string[];
}): JsonLd {
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "WebSite",
        "@id": `${siteUrl}/#website`,
        url: `${siteUrl}/`,
        name: siteTitle,
        description: siteDescription,
        publisher: { "@id": `${siteUrl}/#person` },
      },
      {
        "@type": "Person",
        "@id": `${siteUrl}/#person`,
        name: authorName,
        url: `${siteUrl}/about/`,
        sameAs,
      },
    ],
  };
}

export function getBlogPostingJsonLd({
  url,
  headline,
  description,
  datePublished,
  image,
  authorName,
  siteUrl,
  keywords,
}: {
  url: string;
  headline: string;
  description: string;
  datePublished: string;
  image: string;
  authorName: string;
  siteUrl: string;
  keywords: string[];
}): JsonLd {
  return {
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    "@id": `${url}#article`,
    mainEntityOfPage: {
      "@type": "WebPage",
      "@id": url,
    },
    headline,
    description,
    datePublished,
    image: {
      "@type": "ImageObject",
      url: image,
      width: 1200,
      height: 630,
    },
    author: {
      "@type": "Person",
      "@id": `${siteUrl}/#person`,
      name: authorName,
      url: `${siteUrl}/about/`,
    },
    publisher: {
      "@type": "Person",
      "@id": `${siteUrl}/#person`,
      name: authorName,
    },
    keywords,
  };
}
