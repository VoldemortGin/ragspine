/** Deterministic pastel accent per topology domain (pure helpers). */

export interface DomainTint {
  /** Base accent color (hex, no alpha). */
  accent: string;
  /** Border color (accent with alpha). */
  border: string;
  /** Background tint (accent with low alpha). */
  bg: string;
}

const PALETTE: string[] = [
  '#a5b4fc', // indigo
  '#6ee7b7', // emerald
  '#fcd34d', // amber
  '#7dd3fc', // sky
  '#fda4af', // rose
  '#c4b5fd', // violet
  '#5eead4', // teal
  '#fdba74', // orange
];

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

export function domainTint(domain: string): DomainTint {
  const accent = PALETTE[hashString(domain) % PALETTE.length]!;
  return {
    accent,
    border: `${accent}59`, // ~35% alpha
    bg: `${accent}12`, // ~7% alpha
  };
}
