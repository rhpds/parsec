{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "mlflow.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mlflow.fullname" -}}
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
{{- define "mlflow.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "mlflow.labels" -}}
helm.sh/chart: {{ include "mlflow.chart" . }}
app: mlflow
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Namespace name — defaults to .Release.Namespace if namespace.name is not set.
*/}}
{{- define "mlflow.namespaceName" -}}
{{- default .Release.Namespace .Values.namespace.name -}}
{{- end -}}

{{/*
External hostname for Route.
Uses namespace + ingressDomain, matching the Parsec chart pattern.
*/}}
{{- define "mlflow.externalHostname" -}}
{{- if .Values.ingressDomain -}}
{{ include "mlflow.namespaceName" . }}.{{ .Values.ingressDomain }}
{{- else -}}
{{ include "mlflow.name" . }}.{{ .Release.Namespace }}
{{- end -}}
{{- end -}}

{{/*
External API hostname for Route.
*/}}
{{- define "mlflow.externalApiHostname" -}}
{{- if .Values.ingressDomain -}}
{{ include "mlflow.namespaceName" . }}-api.{{ .Values.ingressDomain }}
{{- else -}}
{{ include "mlflow.name" . }}-api.{{ .Release.Namespace }}
{{- end -}}
{{- end -}}
