/**
 * Built-in workflow templates for the "New workflow" gallery.
 *
 * The non-blank yamls are copies of representative backend fixtures
 * (tests/dify/fixtures at the repo root). Their nodes carry no positions;
 * parseWorkflowYaml auto-layouts position-less nodes on load.
 */

import { serializeWorkflowYaml } from '../../../workflow/convert';
import { nodeRegistry } from '../../../workflow/registry';
import { createTemplateWorkflow } from './template';

export interface WorkflowTemplate {
  id: string;
  name: string;
  description: string;
  /** Dify DSL for the starter document. */
  yaml: string;
}

/* Copy of tests/dify/fixtures/qa_fold.yml (linear RAG: kr -> llm -> answer). */
const RAG_QA_YAML = `app:
  mode: advanced-chat
  name: qa-fold-demo
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: 开始
          variables:
            - {variable: question, label: 问题, type: text-input, required: true}
      - id: kr_1
        data:
          type: knowledge-retrieval
          title: 知识检索
          query_variable_selector: [start_1, question]
          dataset_ids: [ds_hk]
          multiple_retrieval_config: {top_k: 4}
      - id: llm_1
        data:
          type: llm
          title: 应答模型
          model:
            provider: anthropic
            name: claude-opus-4-8
            completion_params: {max_tokens: 512}
          context:
            enabled: true
            variable_selector: [kr_1, result]
          prompt_template:
            - role: user
              text: "根据资料回答：{{#start_1.question#}}"
      - id: answer_1
        data:
          type: answer
          title: 回复
          answer: "{{#llm_1.text#}}"
    edges:
      - {source: start_1, target: kr_1, sourceHandle: source}
      - {source: kr_1, target: llm_1, sourceHandle: source}
      - {source: llm_1, target: answer_1, sourceHandle: source}
`;

/* Copy of tests/dify/fixtures/branch.yml (if-else branch joining an answer). */
const BRANCH_YAML = `app:
  mode: advanced-chat
  name: branch-demo
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: 开始
          variables:
            - variable: score
              label: 分数
              type: number
              required: true
      - id: ifelse_1
        data:
          type: if-else
          title: 阈值判断
          cases:
            - case_id: "true"
              logical_operator: and
              conditions:
                - variable_selector:
                    - start_1
                    - score
                  comparison_operator: ">"
                  value: "60"
      - id: llm_yes
        data:
          type: llm
          title: 通过应答
          model:
            provider: anthropic
            name: claude-opus-4-8
            completion_params:
              max_tokens: 512
          prompt_template:
            - role: user
              text: "恭喜通过，分数 {{#start_1.score#}}"
      - id: llm_no
        data:
          type: llm
          title: 未过应答
          model:
            provider: anthropic
            name: claude-opus-4-8
            completion_params:
              max_tokens: 512
          prompt_template:
            - role: user
              text: "未通过，分数 {{#start_1.score#}}"
      - id: answer_1
        data:
          type: answer
          title: 回复
          answer: "{{#llm_yes.text#}}{{#llm_no.text#}}"
    edges:
      - source: start_1
        target: ifelse_1
        sourceHandle: source
      - source: ifelse_1
        target: llm_yes
        sourceHandle: "true"
      - source: ifelse_1
        target: llm_no
        sourceHandle: "false"
      - source: llm_yes
        target: answer_1
        sourceHandle: source
      - source: llm_no
        target: answer_1
        sourceHandle: source
`;

/* Copy of tests/dify/fixtures/iteration.yml (iteration container over an array). */
const ITERATION_YAML = `app:
  mode: workflow
  name: iteration-demo
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: 开始
          variables:
            - variable: items
              label: 条目列表
              type: array
              required: true
      - id: iter_1
        data:
          type: iteration
          title: 逐项处理
          iterator_selector:
            - start_1
            - items
          output_selector:
            - iter_llm
            - text
          output_type: array[string]
          is_parallel: true
          parallel_nums: 5
          start_node_id: iter_llm
      - id: iter_llm
        data:
          type: llm
          title: 处理单项
          iteration_id: iter_1
          model:
            provider: anthropic
            name: claude-opus-4-8
            completion_params:
              max_tokens: 512
          prompt_template:
            - role: user
              text: "翻译这一项：{{#iter_1.item#}}"
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - variable: results
              value_selector:
                - iter_1
                - output
    edges:
      - source: start_1
        target: iter_1
        sourceHandle: source
      - source: iter_1
        target: end_1
        sourceHandle: source
`;

/* Copy of tests/dify/fixtures/parallel.yml (parallel fan-out + template join). */
const PARALLEL_YAML = `app:
  mode: workflow
  name: parallel-demo
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: 开始
          variables:
            - variable: topic
              label: 主题
              type: text-input
              required: true
      - id: llm_a
        data:
          type: llm
          title: 视角A
          model:
            provider: anthropic
            name: claude-opus-4-8
            completion_params:
              max_tokens: 1024
          prompt_template:
            - role: user
              text: "从正面分析：{{#start_1.topic#}}"
      - id: llm_b
        data:
          type: llm
          title: 视角B
          model:
            provider: anthropic
            name: claude-opus-4-8
            completion_params:
              max_tokens: 1024
          prompt_template:
            - role: user
              text: "从反面分析：{{#start_1.topic#}}"
      - id: tt_join
        data:
          type: template-transform
          title: 合并
          template: "正面：{{ a }}\\n反面：{{ b }}"
          variables:
            - variable: a
              value_selector:
                - llm_a
                - text
            - variable: b
              value_selector:
                - llm_b
                - text
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - variable: result
              value_selector:
                - tt_join
                - output
    edges:
      - source: start_1
        target: llm_a
        sourceHandle: source
      - source: start_1
        target: llm_b
        sourceHandle: source
      - source: llm_a
        target: tt_join
        sourceHandle: source
      - source: llm_b
        target: tt_join
        sourceHandle: source
      - source: tt_join
        target: end_1
        sourceHandle: source
`;

/** Blank first: the gallery's quick-create default. */
export const WORKFLOW_TEMPLATES: readonly WorkflowTemplate[] = [
  {
    id: 'blank',
    name: 'Blank',
    description: 'Minimal starter: start → LLM → end.',
    yaml: serializeWorkflowYaml(createTemplateWorkflow('Blank')),
  },
  {
    id: 'rag-qa',
    name: 'RAG Q&A',
    description:
      'Linear RAG (advanced-chat): knowledge retrieval feeds an LLM as context, replied via an answer node.',
    yaml: RAG_QA_YAML,
  },
  {
    id: 'branch',
    name: 'Conditional branch',
    description: 'An if-else node routes to different LLM prompts, joined by one answer node.',
    yaml: BRANCH_YAML,
  },
  {
    id: 'iteration',
    name: 'Iteration',
    description: 'An iteration container runs an inner LLM over each item of an input array.',
    yaml: ITERATION_YAML,
  },
  {
    id: 'parallel',
    name: 'Parallel fan-out',
    description: 'Two LLMs analyze the same topic concurrently; a template node joins the results.',
    yaml: PARALLEL_YAML,
  },
];

/** Rough node-type badges: scan `type:` strings, keep only known node types. */
export function templateNodeTypes(yaml: string): string[] {
  const types: string[] = [];
  for (const match of yaml.matchAll(/\btype:\s*([a-z][a-z0-9-]*)/g)) {
    const t = match[1]!;
    if (t in nodeRegistry && !types.includes(t)) types.push(t);
  }
  return types;
}
