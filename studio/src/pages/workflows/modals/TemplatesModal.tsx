/** "New workflow" template gallery: Blank plus fixture-derived starters.
 * A single click on a card creates the workflow and closes the modal. */

import { Badge, Modal } from '../../../components';
import { WORKFLOW_TEMPLATES, templateNodeTypes } from '../model/templates';
import type { WorkflowTemplate } from '../model/templates';
import { useEditorStore } from '../store';

export interface TemplatesModalProps {
  open: boolean;
  onClose: () => void;
}

export function TemplatesModal({ open, onClose }: TemplatesModalProps) {
  const createWorkflow = useEditorStore((s) => s.createWorkflow);
  const createFromTemplate = useEditorStore((s) => s.createFromTemplate);

  const pick = (template: WorkflowTemplate) => {
    // Blank keeps the classic quick-new path ("Untitled workflow N" naming).
    if (template.id === 'blank') createWorkflow();
    else createFromTemplate(template);
    onClose();
  };

  return (
    <Modal open={open} onClose={onClose} title="New workflow" size="lg">
      <div className="grid grid-cols-2 gap-3">
        {WORKFLOW_TEMPLATES.map((template) => (
          <button
            key={template.id}
            type="button"
            onClick={() => pick(template)}
            className="rounded-lg border border-white/10 bg-white/[0.02] p-3 text-left transition-colors hover:border-indigo-400/40 hover:bg-white/[0.04]"
          >
            <div className="text-sm font-medium text-zinc-200">{template.name}</div>
            <div className="mt-1 text-xs leading-5 text-zinc-500">{template.description}</div>
            <div className="mt-2 flex flex-wrap gap-1">
              {templateNodeTypes(template.yaml).map((type) => (
                <Badge key={type} className="!px-1.5 font-mono !text-[10px]">
                  {type}
                </Badge>
              ))}
            </div>
          </button>
        ))}
      </div>
    </Modal>
  );
}
