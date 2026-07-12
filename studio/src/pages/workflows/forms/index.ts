/** Registry mapping Dify node types to their property forms. */

import type { ComponentType } from 'react';

import { AnswerForm } from './AnswerForm';
import { CodeForm } from './CodeForm';
import { DocumentExtractorForm } from './DocumentExtractorForm';
import { EndForm } from './EndForm';
import { HttpRequestForm } from './HttpRequestForm';
import { IfElseForm } from './IfElseForm';
import { IterationForm } from './IterationForm';
import { KnowledgeRetrievalForm } from './KnowledgeRetrievalForm';
import { LlmForm } from './LlmForm';
import { LoopForm } from './LoopForm';
import { ParameterExtractorForm } from './ParameterExtractorForm';
import { QuestionClassifierForm } from './QuestionClassifierForm';
import { StartForm } from './StartForm';
import { TemplateTransformForm } from './TemplateTransformForm';
import { ToolForm } from './ToolForm';
import type { NodeFormProps } from './types';
import { UnknownForm } from './UnknownForm';
import { VariableAggregatorForm } from './VariableAggregatorForm';
import { VariableAssignerForm } from './VariableAssignerForm';

export type { NodeFormProps } from './types';

const FORMS: Record<string, ComponentType<NodeFormProps>> = {
  start: StartForm,
  end: EndForm,
  answer: AnswerForm,
  llm: LlmForm,
  code: CodeForm,
  'if-else': IfElseForm,
  'question-classifier': QuestionClassifierForm,
  'template-transform': TemplateTransformForm,
  iteration: IterationForm,
  'knowledge-retrieval': KnowledgeRetrievalForm,
  'parameter-extractor': ParameterExtractorForm,
  tool: ToolForm,
  'http-request': HttpRequestForm,
  'variable-aggregator': VariableAggregatorForm,
  assigner: VariableAssignerForm,
  'document-extractor': DocumentExtractorForm,
  loop: LoopForm,
};

export function getNodeForm(type: string): ComponentType<NodeFormProps> {
  return Object.prototype.hasOwnProperty.call(FORMS, type) ? FORMS[type] : UnknownForm;
}
