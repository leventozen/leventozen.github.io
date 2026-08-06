export function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

type TaxonomyPost = {
  slug: string;
  frontmatter: {
    draft?: boolean;
    external?: boolean;
    categories?: string[];
    tags?: string[];
    date: Date;
    title: string;
  };
};

export type TaxonomyTerm = {
  label: string;
  slug: string;
  count: number;
};

function collectTerms(
  posts: TaxonomyPost[],
  field: "categories" | "tags"
): TaxonomyTerm[] {
  const terms = new Map<string, TaxonomyTerm>();

  for (const post of posts) {
    if (post.frontmatter.draft) continue;

    for (const label of post.frontmatter[field] ?? []) {
      const slug = slugify(label);
      if (!slug) continue;

      const existing = terms.get(slug);
      if (existing) {
        existing.count += 1;
      } else {
        terms.set(slug, { label, slug, count: 1 });
      }
    }
  }

  return Array.from(terms.values()).sort((a, b) =>
    a.label.localeCompare(b.label)
  );
}

export function getCategories(posts: TaxonomyPost[]): TaxonomyTerm[] {
  return collectTerms(posts, "categories");
}

export function getTags(posts: TaxonomyPost[]): TaxonomyTerm[] {
  return collectTerms(posts, "tags");
}

export function getPostsByCategory(posts: TaxonomyPost[], slug: string) {
  return posts
    .filter((post) => !post.frontmatter.draft)
    .filter((post) =>
      (post.frontmatter.categories ?? []).some(
        (category) => slugify(category) === slug
      )
    )
    .sort(
      (a, b) =>
        new Date(b.frontmatter.date).valueOf() -
        new Date(a.frontmatter.date).valueOf()
    );
}

export function getPostsByTag(posts: TaxonomyPost[], slug: string) {
  return posts
    .filter((post) => !post.frontmatter.draft)
    .filter((post) =>
      (post.frontmatter.tags ?? []).some((tag) => slugify(tag) === slug)
    )
    .sort(
      (a, b) =>
        new Date(b.frontmatter.date).valueOf() -
        new Date(a.frontmatter.date).valueOf()
    );
}
