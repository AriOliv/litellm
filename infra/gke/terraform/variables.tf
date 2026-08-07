variable "project_id" {
  description = "Target GCP project id."
  type        = string
}

variable "region" {
  description = "Region for the cluster, Cloud SQL, and the static IP. Keep it where the gateway lives today."
  type        = string
  default     = "southamerica-east1"
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster."
  type        = string
  default     = "litellm-gateway"
}

variable "artifact_repo" {
  description = "Artifact Registry repo name for the gateway image. Leave empty to reuse an existing gcr.io path instead."
  type        = string
  default     = "litellm"
}

variable "static_ip_name" {
  description = "Name of the reserved global static IP the HTTPS load balancer will use."
  type        = string
  default     = "litellm-gateway-ip"
}

variable "db_instance_name" {
  description = "Cloud SQL instance name."
  type        = string
  default     = "litellm-pg"
}

variable "db_tier" {
  description = "Cloud SQL machine tier. Right-size from observed load; this is a starting point."
  type        = string
  default     = "db-custom-2-8192"
}

variable "db_disk_size_gb" {
  description = "Initial Cloud SQL disk size in GB. Autoresize is on, so this only sets the floor (the source DB is ~22 GB)."
  type        = number
  default     = 40
}

variable "db_name" {
  description = "Application database name (matches the current gateway DB)."
  type        = string
  default     = "litellm"
}

variable "db_user" {
  description = "Application database user (matches the current gateway DB)."
  type        = string
  default     = "llmproxy"
}

variable "gateway_ksa" {
  description = "Kubernetes service account name the gateway pods run as (in the app namespace)."
  type        = string
  default     = "litellm"
}

variable "app_namespace" {
  description = "Kubernetes namespace for the gateway."
  type        = string
  default     = "litellm"
}

variable "deletion_protection" {
  description = "Guard the Cloud SQL instance and cluster against accidental terraform destroy."
  type        = bool
  default     = true
}
