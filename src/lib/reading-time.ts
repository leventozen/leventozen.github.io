export const WORDS_PER_MINUTE = 180;

export function getReadingTime(markdown: string): number {
  const prose = markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/{%[\s\S]*?%}/g, " ")
    .replace(/[#>*_~|-]/g, " ");

  const words = prose.trim().match(/\b[\p{L}\p{N}'’-]+\b/gu)?.length ?? 0;
  return Math.max(1, Math.ceil(words / WORDS_PER_MINUTE));
}
