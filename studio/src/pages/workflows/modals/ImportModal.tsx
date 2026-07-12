/** Modal for importing a workflow from pasted or file-picked Dify YAML or
 * n8n JSON. n8n input is detected automatically and converted to Dify DSL by
 * the RAGSpine server (Convert previews warnings + yaml before importing). */

import { useEffect, useRef, useState } from 'react';

import { ApiError, convertN8n } from '../../../api/client';
import { Button, CodeBlock, IconUpload, Modal, Spinner, TextArea } from '../../../components';
import { detectN8nWorkflow } from '../../../workflow/n8n';
import { describeApiError } from '../shared';

export interface ImportModalProps {
  open: boolean;
  onClose: () => void;
  /** Throws (WorkflowParseError or other Error) on invalid input. */
  onImport: (text: string) => void;
}

interface ImportError {
  title: string;
  detail: string;
}

interface Conversion {
  yaml: string;
  warnings: string[];
}

export function ImportModal({ open, onClose, onImport }: ImportModalProps) {
  const [text, setText] = useState('');
  const [fileName, setFileName] = useState<string | null>(null);
  const [error, setError] = useState<ImportError | null>(null);
  const [converting, setConverting] = useState(false);
  const [conversion, setConversion] = useState<Conversion | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setText('');
    setFileName(null);
    setError(null);
    setConversion(null);
  }, [open]);

  /** Replace the source text, resetting any stale conversion/error. */
  const setSource = (content: string, file: string | null) => {
    setText(content);
    setFileName(file);
    setError(null);
    setConversion(null);
  };

  const pickFile = async (file: File) => {
    try {
      const content = await file.text();
      setSource(content, file.name);
    } catch (err) {
      setError({
        title: 'Import failed',
        detail: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const convert = () => {
    const workflow = detectN8nWorkflow(text);
    if (workflow === null) return;
    setConverting(true);
    setError(null);
    Promise.resolve()
      .then(() => convertN8n('n8n_to_dify', workflow))
      .then((res) => {
        if (res.yaml === null || res.yaml.trim() === '') {
          throw new Error('The server returned no converted YAML.');
        }
        setConversion({ yaml: res.yaml, warnings: res.warnings });
      })
      .catch((err: unknown) => {
        const detail =
          err instanceof ApiError && err.type === 'network_error'
            ? 'Could not reach the RAGSpine server. Importing n8n workflows requires the server to be online.'
            : describeApiError(err).message;
        setError({ title: 'n8n conversion failed', detail });
      })
      .finally(() => setConverting(false));
  };

  const handleImport = () => {
    try {
      onImport(conversion !== null ? conversion.yaml : text);
    } catch (err) {
      setError({
        title: 'Import failed',
        detail: err instanceof Error ? err.message : String(err),
      });
      return;
    }
    setSource('', null);
    onClose();
  };

  const needsConvert = conversion === null && detectN8nWorkflow(text) !== null;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Import workflow"
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          {needsConvert ? (
            <Button variant="primary" disabled={converting} onClick={convert}>
              {converting && <Spinner size="sm" />}
              Convert n8n workflow
            </Button>
          ) : (
            <Button variant="primary" disabled={text.trim() === ''} onClick={handleImport}>
              Import
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-3">
        <div className="flex min-w-0 items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".yml,.yaml,.json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file !== undefined) void pickFile(file);
              e.target.value = '';
            }}
          />
          <Button variant="secondary" size="sm" onClick={() => fileRef.current?.click()}>
            <IconUpload size={13} />
            Choose file
          </Button>
          {fileName !== null ? (
            <span className="truncate font-mono text-[11px] text-zinc-400">{fileName}</span>
          ) : (
            <span className="text-[11px] text-zinc-500">
              or paste Dify workflow YAML / n8n workflow JSON below
            </span>
          )}
        </div>
        <TextArea
          rows={conversion !== null ? 6 : 12}
          value={text}
          placeholder={'app:\n  mode: workflow\n  ...'}
          onChange={(e) => setSource(e.target.value, fileName)}
          className="font-mono !text-xs"
        />
        {needsConvert && (
          <div className="text-[11px] leading-4 text-zinc-500">
            n8n workflow detected — it will be converted to Dify DSL by the RAGSpine server before
            import.
          </div>
        )}
        {conversion !== null && (
          <>
            {conversion.warnings.length > 0 && (
              <div className="rounded-lg border border-amber-400/25 bg-amber-400/[0.06] px-4 py-3">
                <div className="text-sm font-medium text-amber-300">Conversion warnings</div>
                <ul className="mt-1.5 list-disc space-y-1 pl-4 text-xs leading-5 text-zinc-400">
                  {conversion.warnings.map((warning, i) => (
                    <li key={i}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
            <CodeBlock code={conversion.yaml} language="converted yaml" maxHeight="14rem" />
          </>
        )}
        {error !== null && (
          <div className="rounded-lg border border-red-400/25 bg-red-400/[0.06] px-4 py-3">
            <div className="text-sm font-medium text-red-300">{error.title}</div>
            <div className="mt-1 text-xs leading-5 whitespace-pre-wrap text-zinc-400">
              {error.detail}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
