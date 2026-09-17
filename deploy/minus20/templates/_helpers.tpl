{{- define "minus20.labels" -}}
app.kubernetes.io/part-of: minus20
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/*
Render a Telegram chat id as plain digits. Helm turns a numeric --set value
into a float once it has been stored and reused (--reuse-values, or a values
file), and `quote` then prints 1.94698214e+08. Telegram happens to accept
that for sending, but the agent's allow-list compares strings and silently
ignored its own owner. Accepts a number, a float, or a comma-separated string.
*/}}
{{- define "minus20.chatId" -}}
{{- $v := . -}}
{{- if kindIs "string" $v -}}
{{- $v -}}
{{- else if $v -}}
{{- printf "%.0f" (float64 $v) -}}
{{- end -}}
{{- end -}}
