/** Node-type icon mapping + the colored type chip used across the editor. */

import type { ComponentType } from 'react';

import {
  IconBraces,
  IconCode,
  IconDatabase,
  IconFlag,
  IconGitBranch,
  IconListFilter,
  IconMessageSquare,
  IconPlay,
  IconRepeat,
  IconScanText,
  IconSparkle,
  IconWorkflow,
  IconWrench,
  cn,
} from '../../components';
import type { IconProps } from '../../components';
import { getNodeDefinition } from '../../workflow/registry';

const ICONS: Record<string, ComponentType<IconProps>> = {
  start: IconPlay,
  end: IconFlag,
  answer: IconMessageSquare,
  llm: IconSparkle,
  code: IconCode,
  'if-else': IconGitBranch,
  'question-classifier': IconListFilter,
  'template-transform': IconBraces,
  iteration: IconRepeat,
  'knowledge-retrieval': IconDatabase,
  'parameter-extractor': IconScanText,
  tool: IconWrench,
};

export function nodeTypeIcon(type: string): ComponentType<IconProps> {
  return ICONS[type] ?? IconWorkflow;
}

export interface TypeChipProps {
  type: string;
  size?: 'sm' | 'md';
  className?: string;
}

/** Colored square icon chip tinted with the node type's registry accent. */
export function TypeChip({ type, size = 'md', className }: TypeChipProps) {
  const def = getNodeDefinition(type);
  const Icon = nodeTypeIcon(type);
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center justify-center',
        size === 'sm' ? 'h-5 w-5 rounded' : 'h-7 w-7 rounded-md',
        className,
      )}
      style={{ backgroundColor: `${def.accent}1f`, color: def.accent }}
    >
      <Icon size={size === 'sm' ? 11 : 14} />
    </span>
  );
}
