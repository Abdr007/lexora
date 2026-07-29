variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "Cloud Run region. Any always-free region works."
}

variable "service_name" {
  type    = string
  default = "lexora-api"
}

variable "image" {
  type        = string
  description = "Container image URL, e.g. europe-west1-docker.pkg.dev/PROJECT/lexora/api:latest"
}

variable "cors_allow_origins" {
  type        = string
  default     = "https://lexora.vercel.app"
  description = "Comma-separated allowlist. Never '*'."
}

variable "anthropic_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional. Left empty, the service runs in offline-extractive mode."
}

variable "max_instances" {
  type        = number
  default     = 3
  description = "Caps worst-case spend if the demo is shared widely."
}
