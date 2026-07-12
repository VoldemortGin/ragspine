/** State slice for the Analyze action (shared by toolbar, drawer, canvas). */

import type { Suggestion } from '../../../api/types';

export type AnalysisSlice =
  | { status: 'loading' }
  | { status: 'done'; requestId: string; suggestions: Suggestion[] }
  | { status: 'error'; error: unknown };
