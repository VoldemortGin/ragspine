{{/* 通用 chart 名（可被 nameOverride 覆盖）。 */}}
{{- define "ragspine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* fullname：release 名 + chart 名拼装，作为所有资源名前缀（可被 fullnameOverride 覆盖）。 */}}
{{- define "ragspine.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* chart 标签值（name-version）。 */}}
{{- define "ragspine.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 标准公共标签。 */}}
{{- define "ragspine.labels" -}}
helm.sh/chart: {{ include "ragspine.chart" . }}
{{ include "ragspine.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* selector 标签（Deployment selector 与 Service selector 须稳定，不含 version）。 */}}
{{- define "ragspine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ragspine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* 非机密环境 ConfigMap 名。 */}}
{{- define "ragspine.envConfigMapName" -}}
{{- printf "%s-env" (include "ragspine.fullname" .) -}}
{{- end -}}

{{/* 机密 Secret 名。 */}}
{{- define "ragspine.secretName" -}}
{{- printf "%s-secrets" (include "ragspine.fullname" .) -}}
{{- end -}}

{{/* 是否需要渲染 Secret：任一 API key 非空时为 true。 */}}
{{- define "ragspine.hasSecrets" -}}
{{- if or .Values.secrets.anthropicApiKey .Values.secrets.openaiApiKey -}}true{{- end -}}
{{- end -}}

{{/* server 与 worker 共用的 envFrom：非机密 ConfigMap +（按需）机密 Secret。 */}}
{{- define "ragspine.envFrom" -}}
- configMapRef:
    name: {{ include "ragspine.envConfigMapName" . }}
{{- if include "ragspine.hasSecrets" . }}
- secretRef:
    name: {{ include "ragspine.secretName" . }}
{{- end }}
{{- end -}}

{{/* 集群内 redis 服务名。 */}}
{{- define "ragspine.redisFullname" -}}
{{- printf "%s-redis" (include "ragspine.fullname" .) -}}
{{- end -}}

{{/* 集群内 postgres 服务名。 */}}
{{- define "ragspine.postgresFullname" -}}
{{- printf "%s-postgres" (include "ragspine.fullname" .) -}}
{{- end -}}

{{/* 集群内 qdrant 服务名。 */}}
{{- define "ragspine.qdrantFullname" -}}
{{- printf "%s-qdrant" (include "ragspine.fullname" .) -}}
{{- end -}}
