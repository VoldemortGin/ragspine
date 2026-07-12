/** Property form for `http-request` nodes: method, url, headers, body, auth. */

import { Checkbox, Field, Select, TextInput } from '../../../components';
import type {
  HttpAuthorization,
  HttpAuthorizationConfig,
  HttpBody,
  HttpRequestNodeData,
} from '../../../workflow/types';
import { VariableTextArea } from '../VariableTextArea';
import { FormHint } from '../shared';
import type { NodeFormProps } from './types';

const METHODS = ['get', 'post', 'put', 'delete', 'patch', 'head'];
const BODY_TYPES = ['none', 'json', 'raw-text', 'x-www-form-urlencoded', 'form-data', 'binary'];
const RAW_BODY_TYPES = ['json', 'raw-text', 'x-www-form-urlencoded'];

function asAuth(value: unknown): HttpAuthorization {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as HttpAuthorization)
    : { type: 'no-auth', config: null };
}

function asBody(value: unknown): HttpBody {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as HttpBody)
    : { type: 'none', data: [] };
}

export function HttpRequestForm({ data, available, onChange }: NodeFormProps) {
  const typed = data as HttpRequestNodeData;
  const method = typeof typed.method === 'string' ? typed.method : 'get';
  const methodOptions = METHODS.includes(method) ? METHODS : [...METHODS, method];

  const auth = asAuth(typed.authorization);
  const authType = auth.type === 'api-key' ? 'api-key' : 'no-auth';
  const authConfig: HttpAuthorizationConfig =
    typeof auth.config === 'object' && auth.config !== null ? auth.config : {};
  const patchAuth = (changes: Partial<HttpAuthorization>) =>
    onChange({ ...data, authorization: { ...auth, ...changes } });
  const patchAuthConfig = (changes: Partial<HttpAuthorizationConfig>) =>
    onChange({ ...data, authorization: { ...auth, config: { ...authConfig, ...changes } } });

  const body = asBody(typed.body);
  const bodyType = typeof body.type === 'string' ? body.type : 'none';
  const bodyTypeOptions = BODY_TYPES.includes(bodyType) ? BODY_TYPES : [...BODY_TYPES, bodyType];
  const bodyData = typeof body.data === 'string' ? body.data : '';

  const changeBodyType = (next: string) => {
    // Switching into a raw type needs a string payload; preserve any existing
    // string, otherwise start empty (the array shape belongs to form-data).
    const nextData = RAW_BODY_TYPES.includes(next) ? bodyData : body.data;
    onChange({ ...data, body: { ...body, type: next, data: nextData } });
  };

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-[5rem_1fr] gap-2">
        <Field label="Method">
          <Select
            value={method}
            onChange={(e) => onChange({ ...data, method: e.target.value as HttpRequestNodeData['method'] })}
            className="h-7.5 !text-xs uppercase"
          >
            {methodOptions.map((m) => (
              <option key={m} value={m}>
                {m.toUpperCase()}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="URL">
          <VariableTextArea
            value={typeof typed.url === 'string' ? typed.url : ''}
            rows={1}
            placeholder="https://api.example.com/v1/{{#start_1.path#}}"
            available={available}
            onChange={(url) => onChange({ ...data, url })}
            className="font-mono !text-xs"
          />
        </Field>
      </div>

      <Field label="Headers" hint="One 'Key: Value' per line.">
        <VariableTextArea
          value={typeof typed.headers === 'string' ? typed.headers : ''}
          rows={2}
          placeholder="Content-Type: application/json"
          available={available}
          onChange={(headers) => onChange({ ...data, headers })}
          className="font-mono !text-xs"
        />
      </Field>

      <Field label="Query params" hint="One 'Key: Value' per line.">
        <VariableTextArea
          value={typeof typed.params === 'string' ? typed.params : ''}
          rows={2}
          placeholder="q: {{#start_1.query#}}"
          available={available}
          onChange={(params) => onChange({ ...data, params })}
          className="font-mono !text-xs"
        />
      </Field>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Body</div>
        <div className="space-y-1.5 rounded-md border border-white/5 bg-white/[0.02] p-2">
          <Select
            value={bodyType}
            onChange={(e) => changeBodyType(e.target.value)}
            className="h-7.5 !text-xs"
          >
            {bodyTypeOptions.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
          {RAW_BODY_TYPES.includes(bodyType) && (
            <VariableTextArea
              value={bodyData}
              rows={4}
              placeholder={bodyType === 'json' ? '{ "q": "{{#start_1.query#}}" }' : 'raw body'}
              available={available}
              onChange={(next) => onChange({ ...data, body: { ...body, data: next } })}
              className="font-mono !text-xs"
            />
          )}
          {(bodyType === 'form-data' || bodyType === 'binary') && (
            <FormHint>
              {bodyType} body items are preserved from import; edit them via Export YAML for now.
            </FormHint>
          )}
        </div>
      </div>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Authorization</div>
        <div className="space-y-1.5 rounded-md border border-white/5 bg-white/[0.02] p-2">
          <Select
            value={authType}
            onChange={(e) =>
              patchAuth({ type: e.target.value as HttpAuthorization['type'] })
            }
            className="h-7.5 !text-xs"
          >
            <option value="no-auth">No auth</option>
            <option value="api-key">API key</option>
          </Select>
          {authType === 'api-key' && (
            <>
              <div className="grid grid-cols-2 gap-1.5">
                <Select
                  value={typeof authConfig.type === 'string' ? authConfig.type : 'bearer'}
                  onChange={(e) =>
                    patchAuthConfig({ type: e.target.value as HttpAuthorizationConfig['type'] })
                  }
                  className="h-7.5 !text-xs"
                >
                  <option value="basic">basic</option>
                  <option value="bearer">bearer</option>
                  <option value="custom">custom</option>
                </Select>
                {authConfig.type === 'custom' && (
                  <TextInput
                    value={typeof authConfig.header === 'string' ? authConfig.header : ''}
                    placeholder="header name"
                    onChange={(e) => patchAuthConfig({ header: e.target.value })}
                    className="h-7.5 font-mono !text-xs"
                  />
                )}
              </div>
              <TextInput
                value={typeof authConfig.api_key === 'string' ? authConfig.api_key : ''}
                placeholder="api key / token"
                onChange={(e) => patchAuthConfig({ api_key: e.target.value })}
                className="h-7.5 font-mono !text-xs"
              />
            </>
          )}
        </div>
      </div>

      <Checkbox
        checked={typed.ssl_verify !== false}
        onChange={(e) => onChange({ ...data, ssl_verify: e.target.checked })}
        label="Verify TLS certificate"
        className="!text-xs"
      />
      <FormHint>
        HTTP requests run only when the server is started with RAGSPINE_DIFY_HTTP_ENABLED=true.
      </FormHint>
    </div>
  );
}
