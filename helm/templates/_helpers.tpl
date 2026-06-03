{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "parsec.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "parsec.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "parsec.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "parsec.labels" -}}
helm.sh/chart: {{ include "parsec.chart" . }}
{{ include "parsec.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app: {{ include "parsec.name" . }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "parsec.selectorLabels" -}}
app.kubernetes.io/name: {{ include "parsec.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Create the name of the service account to use.
Defaults to "<name>-oauth" matching playbook's parsec-oauth SA.
*/}}
{{- define "parsec.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
    {{ default (printf "%s-oauth" (include "parsec.name" .)) .Values.serviceAccount.name }}
{{- else -}}
    {{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Namespace name
*/}}
{{- define "parsec.namespaceName" -}}
{{- default .Release.Namespace .Values.namespace.name -}}
{{- end -}}

{{/*
Container image. When buildConfig is enabled and no repository is set,
the image comes from the ImageStream (OpenShift injects it via trigger annotation).
Otherwise, build the image URI from repository + version/tag.
*/}}
{{- define "parsec.image" -}}
{{- if and .Values.buildConfig.enabled (not .Values.image.repository) -}}
{{- printf "%s:latest" (include "parsec.name" .) -}}
{{- else if eq .Values.version "main" -}}
{{- printf "%s:latest" .Values.image.repository -}}
{{- else if eq (default "" .Values.image.tagOverride) "-" -}}
{{- .Values.image.repository -}}
{{- else if .Values.image.tagOverride -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tagOverride -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.version -}}
{{- end -}}
{{- end -}}

{{/*
External hostname for Route.
Uses namespace + ingressDomain, matching playbook's target_namespace.cluster_domain pattern.
*/}}
{{- define "parsec.externalHostname" -}}
{{- if .Values.ingressDomain -}}
{{ include "parsec.namespaceName" . }}.{{ .Values.ingressDomain }}
{{- else -}}
{{ include "parsec.name" . }}.{{ .Release.Namespace }}
{{- end -}}
{{- end -}}
