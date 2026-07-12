/**
 * n8n workflow JSON detection. Pure functions, React-free.
 *
 * An n8n export is a JSON object with a `nodes` array and a `connections`
 * object; anything else (Dify YAML included) is not detected as n8n.
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Parse `text` as an n8n workflow JSON export.
 * Returns the parsed object, or null when the text is not n8n JSON.
 */
export function detectN8nWorkflow(text: string): Record<string, unknown> | null {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isRecord(doc)) return null;
  if (!Array.isArray(doc['nodes'])) return null;
  if (!isRecord(doc['connections'])) return null;
  return doc;
}
